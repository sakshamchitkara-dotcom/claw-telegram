"""Offline mock backend for tests and dry runs.

- Streams "echo: <text>" word by word.
- "!task <action>" pretends to be an agent that wants to run a side-effecting
  action: it emits an ApprovalRequest and blocks until it is resolved.
- "!fail" raises a BackendError.
"""

from __future__ import annotations

import asyncio
import itertools

from .base import ApprovalRequest, Backend, BackendError, Status, TextDelta, Turn


class EchoBackend(Backend):
    name = "echo"
    supports_images = True

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._ids = itertools.count(1)

    async def stream(self, turn: Turn):
        text = turn.text.strip()
        if text == "!fail":
            raise BackendError("mock failure requested")
        if text.startswith("!task "):
            action = text[6:].strip()
            ref = f"mock-{next(self._ids)}"
            fut = asyncio.get_running_loop().create_future()
            self._pending[ref] = fut
            yield Status(f"planning: {action}")
            yield ApprovalRequest(ref=ref, summary=f"run shell: {action}")
            try:
                approved = await fut
            finally:
                self._pending.pop(ref, None)
            yield TextDelta(f"Executed `{action}` (mock)." if approved else f"Skipped `{action}`: denied.")
            return
        if turn.images:
            yield TextDelta(f"received {len(turn.images)} image(s): "
                            + ", ".join(f"{i.mime} {len(i.data)}B" for i in turn.images) + "\n")
        words = f"echo: {text}".split(" ")
        for i, w in enumerate(words):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield TextDelta(w if i == 0 else " " + w)
        yield TextDelta(f"\n(history: {len(turn.history)} msgs)")

    async def resolve_approval(self, ref: str, approve: bool) -> None:
        fut = self._pending.get(ref)
        if fut is None or fut.done():
            raise BackendError(f"no pending approval {ref}")
        fut.set_result(approve)
