"""OpenClaw gateway adapter.

Chat (confirmed, docs/gateway/openai-http-api.md):
- POST /v1/chat/completions on the gateway port (default 18789) once
  gateway.http.endpoints.chatCompletions.enabled = true.
- Authorization: Bearer <gateway token or password>.
- model = "openclaw/default" | "openclaw/<agentId>" selects an agent, not a raw model.
- A stable OpenAI `user` string makes the gateway keep one agent session per
  conversation, so we send only the newest user message plus `user`.
- image_url parts from the latest user message are accepted (8 max, 20 MB).
- GET /v1/models lists agent targets.

Exec approvals (confirmed protocol shape, docs/gateway/protocol/*.md and
packages/gateway-protocol/src/schema/exec-approvals.ts):
- WebSocket on the same port; first frame is a `connect` req (protocol v4).
- Trusted local backend clients (client.id "gateway-client", mode "backend")
  may omit device identity on direct loopback with the shared token.
- Gateway broadcasts `exec.approval.requested`; operators answer with
  `exec.approval.resolve {id, decision}` (scope operator.approvals),
  decisions "allow-once" | "allow-always" | "deny".
- The client must declare caps ["exec-approvals"] to receive the event
  (found in the gateway source; observed against a live 2026.9.6 gateway).
- Observed payload: {"approvalKind": "exec", "id", "request": {"command",
  "host", "allowedDecisions", "sessionKey", ...}, "createdAtMs", "expiresAtMs"}.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import platform
from collections.abc import Awaitable, Callable

import aiohttp

from .. import __version__
from .base import ApprovalRequest, BackendError, Turn
from .openai_compat import OpenAICompatBackend

log = logging.getLogger(__name__)

ApprovalSink = Callable[[ApprovalRequest], Awaitable[None]]
ResolvedSink = Callable[[str, str], Awaitable[None]]  # (approval id, decision)


class OpenClawBackend(OpenAICompatBackend):
    name = "openclaw"
    stateful = True

    def __init__(self, url: str, token: str, agent: str = "openclaw/default", approvals_ws: bool = False, **kw):
        self.root = url.rstrip("/")
        super().__init__(f"{self.root}/v1", agent, api_key=token, **kw)
        self.approvals_ws = approvals_ws
        self.approval_sink: ApprovalSink | None = None
        self.resolved_sink: ResolvedSink | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_task: asyncio.Task | None = None
        self._ws_ready = asyncio.Event()
        self._pending_rpc: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)

    def body(self, turn: Turn) -> dict:
        body = super().body(turn)
        body["user"] = turn.session_id  # stable per chat until /reset
        return body

    # ---- exec approvals over the gateway WebSocket -------------------------

    def start(self) -> None:
        if self.approvals_ws and self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_loop(), name="openclaw-approvals")

    async def _ws_loop(self) -> None:
        ws_url = "ws" + self.root[4:] if self.root.startswith("http") else self.root
        backoff = 1
        while True:
            try:
                async with self.session.ws_connect(ws_url, heartbeat=30, max_msg_size=32 * 1024 * 1024) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    backoff = 1
                    log.info("openclaw: approvals websocket connected")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._on_frame(json.loads(msg.data))
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep reconnecting; approvals must not silently die
                log.warning("openclaw: approvals websocket error: %s", e)
            finally:
                self._ws = None
                self._ws_ready.clear()
                for fut in self._pending_rpc.values():
                    if not fut.done():
                        fut.set_exception(BackendError("openclaw websocket closed"))
                self._pending_rpc.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:  # the gateway normally sends connect.challenge first
            first = await asyncio.wait_for(ws.receive_json(), timeout=5)
            log.debug("openclaw: pre-connect frame %s", first.get("event"))
        except asyncio.TimeoutError:
            pass
        req_id = f"c{next(self._ids)}"
        await ws.send_json({
            "type": "req", "id": req_id, "method": "connect",
            "params": {
                "minProtocol": 4, "maxProtocol": 4,
                "client": {"id": "gateway-client", "mode": "backend", "version": __version__,
                           "platform": platform.system().lower()},
                "role": "operator",
                # Verified against gateway 2026.9.6: exec.approval.requested is only delivered to
                # clients declaring the "exec-approvals" cap, and approvals bound to another
                # device/session are only visible with operator.admin. The shared gateway token
                # already carries owner authority, so asking for admin adds no real privilege.
                "scopes": ["operator.read", "operator.approvals", "operator.admin"],
                "caps": ["exec-approvals"], "commands": [], "permissions": {},
                "auth": {"token": self.api_key},
                "userAgent": f"claw-telegram/{__version__}",
            },
        })
        while True:
            frame = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if frame.get("type") == "res" and frame.get("id") == req_id:
                if not frame.get("ok"):
                    raise BackendError(f"openclaw connect rejected: {frame.get('error')}")
                self._ws_ready.set()
                return

    async def _on_frame(self, frame: dict) -> None:
        if frame.get("type") == "res":
            fut = self._pending_rpc.pop(frame.get("id"), None)
            if fut and not fut.done():
                if frame.get("ok"):
                    fut.set_result(frame.get("payload"))
                else:
                    fut.set_exception(BackendError(f"openclaw rpc error: {frame.get('error')}"))
        elif frame.get("type") == "event" and frame.get("event") == "exec.approval.resolved":
            payload = frame.get("payload") or {}
            if self.resolved_sink and payload.get("id"):
                await self.resolved_sink(str(payload["id"]), str(payload.get("decision") or "expired"))
        elif frame.get("type") == "event" and frame.get("event") == "exec.approval.requested":
            payload = frame.get("payload") or {}
            req = payload.get("request") or {}
            approval_id = payload.get("id")
            if not approval_id:
                return
            command = req.get("command") or req.get("rawCommand") or "(command hidden by gateway)"
            summary = f"exec on {req.get('host', 'gateway')}: {command}"
            if self.approval_sink is None:
                log.warning("openclaw: approval %s requested but no sink registered", approval_id)
                return
            await self.approval_sink(ApprovalRequest(ref=str(approval_id), summary=summary))

    async def _rpc(self, method: str, params: dict) -> object:
        if self._ws is None or not self._ws_ready.is_set():
            raise BackendError("openclaw approvals websocket is not connected")
        req_id = f"r{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending_rpc[req_id] = fut
        await self._ws.send_json({"type": "req", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=15)

    async def resolve_approval(self, ref: str, approve: bool) -> None:
        await self._rpc("exec.approval.resolve", {"id": ref, "decision": "allow-once" if approve else "deny"})

    async def health(self) -> str:
        h = await super().health()
        if self.approvals_ws:
            h += "; approvals ws " + ("connected" if self._ws_ready.is_set() else "down")
        return h

    async def close(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        await super().close()
