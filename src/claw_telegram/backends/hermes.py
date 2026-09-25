"""Nous Research Hermes Agent API server adapter.

Confirmed from the Hermes Agent docs / source (gateway/platforms/api_server*.py):
- POST /v1/chat/completions, Bearer API_SERVER_KEY, default port 8642,
  model "hermes-agent"; the endpoint is stateless (full messages array).
- Streaming adds `event: hermes.tool.progress` frames
  ({"tool", "label", "emoji", "status": "running"|"completed", ...}) and
  `event: approval.request` frames when a tool is gated behind approval
  ({"run_id", "command", "description", "choices", "request_id"?, ...}).
- A pending approval is resolved with POST /v1/runs/{run_id}/approval and
  {"choice": "once"|"deny", "request_id"?}; the SSE stream then resumes.
- X-Hermes-Session-Key gives a stable per-channel memory scope.
- GET /health is a liveness probe.
"""

from __future__ import annotations

import asyncio

import aiohttp

from .base import ApprovalRequest, BackendError, Event, Status, Turn, error_text
from .openai_compat import OpenAICompatBackend


class HermesBackend(OpenAICompatBackend):
    name = "hermes"

    def __init__(self, url: str, api_key: str, model: str = "hermes-agent", **kw):
        if not api_key:
            raise ValueError("hermes: HERMES_API_KEY is required (the API server refuses keyless access)")
        self.root = url.rstrip("/")
        super().__init__(f"{self.root}/v1", model, api_key=api_key, **kw)

    def headers(self, turn: Turn | None) -> dict[str, str]:
        h = super().headers(turn)
        if turn is not None:
            topic = f":{turn.thread_id}" if turn.thread_id else ""
            h["X-Hermes-Session-Key"] = f"telegram:{turn.chat_id}{topic}"
        return h

    def on_event(self, event: str, payload: dict) -> Event | None:
        if event == "hermes.tool.progress" and payload.get("status") == "running":
            return Status(f"{payload.get('emoji', '')} {payload.get('label') or payload.get('tool')}".strip())
        if event == "approval.request":
            run_id = payload.get("run_id")
            if not run_id:
                return None
            ref = f"{run_id}|{payload.get('request_id') or ''}"
            what = payload.get("description") or "approval required"
            if payload.get("command"):
                what += f"\n{payload['command']}"
            return ApprovalRequest(ref=ref, summary=what)
        return None

    async def resolve_approval(self, ref: str, approve: bool) -> None:
        run_id, _, request_id = ref.partition("|")
        body = {"choice": "once" if approve else "deny"}
        if request_id:
            body["request_id"] = request_id
        async with self.session.post(f"{self.base_url}/runs/{run_id}/approval", json=body,
                                     headers=self.headers(None), timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                raise BackendError(f"hermes approval: {error_text(resp.status, await resp.text())}")

    async def health(self) -> str:
        try:
            async with self.session.get(f"{self.root}/health", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                live = "live" if resp.status == 200 else f"HTTP {resp.status}"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return f"unreachable ({e.__class__.__name__})"
        return f"{live}; models: {await super().health()}"
