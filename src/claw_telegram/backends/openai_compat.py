"""Streaming client for any OpenAI-compatible POST /v1/chat/completions endpoint.

Works as-is for Hermes models served by Ollama, vLLM, llama.cpp or OpenRouter.
The OpenClaw and Hermes Agent adapters subclass it and only change headers,
the request body and how their extra SSE event types are interpreted.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import aiohttp

from .base import Backend, BackendError, Event, Status, TextDelta, Turn, error_text, sse_events

log = logging.getLogger(__name__)


class OpenAICompatBackend(Backend):
    name = "openai"
    supports_images = True

    def __init__(self, base_url: str, model: str, api_key: str = "", system_prompt: str = "",
                 session: aiohttp.ClientSession | None = None, timeout_s: float = 600):
        if not base_url or not model:
            raise ValueError(f"{self.name}: base URL and model are required")
        self.base_url = base_url.rstrip("/")  # ends in /v1
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout_s, sock_read=timeout_s)

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    def headers(self, turn: Turn | None) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @staticmethod
    def user_content(turn: Turn) -> str | list[dict]:
        if not turn.images:
            return turn.text
        parts: list[dict] = [{"type": "text", "text": turn.text or "(image)"}]
        for img in turn.images:
            url = f"data:{img.mime};base64,{base64.b64encode(img.data).decode()}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts

    def messages(self, turn: Turn) -> list[dict]:
        msgs: list[dict] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        if not self.stateful:
            msgs.extend(turn.history)
        msgs.append({"role": "user", "content": self.user_content(turn)})
        return msgs

    def body(self, turn: Turn) -> dict:
        return {"model": self.model, "messages": self.messages(turn), "stream": True}

    def on_event(self, event: str, payload: dict) -> Event | None:
        """Hook for vendor-specific named SSE events."""
        return None

    async def stream(self, turn: Turn):
        url = f"{self.base_url}/chat/completions"
        try:
            async with self.session.post(url, json=self.body(turn), headers=self.headers(turn),
                                         timeout=self._timeout) as resp:
                if resp.status != 200:
                    raise BackendError(f"{self.name}: {error_text(resp.status, await resp.text())}")
                async for event, data in sse_events(resp.content):
                    if data.strip() == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except ValueError:
                        log.debug("%s: skipping non-JSON SSE data %r", self.name, data[:200])
                        continue
                    if event and event != "message":
                        mapped = self.on_event(event, payload)
                        if mapped is not None:
                            yield mapped
                        continue
                    if "error" in payload:
                        err = payload["error"]
                        raise BackendError(f"{self.name}: {err.get('message', err) if isinstance(err, dict) else err}")
                    for choice in payload.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield TextDelta(delta["content"])
                        elif delta.get("reasoning") or delta.get("reasoning_content"):
                            # Ollama sends `reasoning`, vLLM/DeepSeek `reasoning_content`; the text itself
                            # isn't shown, but it proves the model is alive and tells the user why it's slow
                            yield Status("thinking…")
                        for call in delta.get("tool_calls") or []:
                            name = (call.get("function") or {}).get("name")
                            if name:
                                yield Status(f"tool call: {name}")
        except asyncio.TimeoutError as e:
            raise BackendError(f"{self.name}: timed out waiting for {self.base_url}") from e
        except aiohttp.ClientError as e:
            raise BackendError(f"{self.name}: cannot reach {self.base_url} ({e.__class__.__name__}: {e})") from e

    async def health(self) -> str:
        try:
            async with self.session.get(f"{self.base_url}/models", headers=self.headers(None),
                                        timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status != 200:
                    return f"error {error_text(resp.status, await resp.text())}"
                data = await resp.json(content_type=None)
                ids = [m.get("id") for m in data.get("data", [])]
                return f"ok ({len(ids)} models{', ' + self.model + ' present' if self.model in ids else ''})"
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return f"unreachable ({e.__class__.__name__})"

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
