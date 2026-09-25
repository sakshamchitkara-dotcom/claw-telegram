"""Backend adapter contract.

A backend turns one user turn into a stream of events. The bot renders
TextDelta by editing a Telegram message, Status as a transient progress line,
and ApprovalRequest as an Approve/Deny keyboard; the user's choice goes back
through ``resolve_approval``. The backend's own stream is expected to pause
until then (that is how Hermes runs and OpenClaw exec approvals behave).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class TextDelta:
    text: str


@dataclass
class Status:
    text: str


@dataclass
class ApprovalRequest:
    ref: str  # backend-specific id needed to resolve it
    summary: str  # what will happen if approved (shown to the user)


Event = TextDelta | Status | ApprovalRequest


@dataclass
class Image:
    mime: str
    data: bytes


@dataclass
class Turn:
    chat_id: int
    session_id: str  # stable per chat, rotates on /reset
    text: str
    history: list[dict] = field(default_factory=list)  # prior [{"role","content"}] text turns
    images: list[Image] = field(default_factory=list)
    thread_id: int = 0  # forum topic, 0 = none


class BackendError(Exception):
    pass


class Backend(ABC):
    name: str = "?"
    supports_images: bool = False
    # Stateful backends keep the transcript server-side keyed by session id,
    # so only the newest user turn is sent.
    stateful: bool = False

    @abstractmethod
    def stream(self, turn: Turn) -> AsyncIterator[Event]: ...

    async def resolve_approval(self, ref: str, approve: bool) -> None:
        raise BackendError(f"{self.name} does not support approvals")

    async def health(self) -> str:
        return "ok"

    async def close(self) -> None:  # noqa: B027 - optional hook
        pass


def sse_events(lines: AsyncIterator[bytes]) -> AsyncIterator[tuple[str | None, str]]:
    """Parse a text/event-stream body into (event_name, data) pairs."""

    async def gen():
        event, data = None, []
        async for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                if data:
                    yield event, "\n".join(data)
                event, data = None, []
            elif line.startswith(":"):
                continue  # comment / keepalive
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
        if data:
            yield event, "\n".join(data)

    return gen()


def error_text(status: int, body: str) -> str:
    try:
        err = json.loads(body).get("error")
        msg = err.get("message") if isinstance(err, dict) else err
    except (ValueError, AttributeError):
        msg = None
    return f"HTTP {status}: {(msg or body)[:300]}"
