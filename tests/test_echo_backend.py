import asyncio

import pytest

from claw_telegram.backends.base import ApprovalRequest, BackendError, TextDelta, Turn
from claw_telegram.backends.echo import EchoBackend


async def collect(it):
    return [e async for e in it]


async def test_echo_streams_words():
    events = await collect(EchoBackend().stream(Turn(1, "s", "hello world")))
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert text.startswith("echo: hello world")


async def test_task_waits_for_approval():
    b = EchoBackend()
    it = b.stream(Turn(1, "s", "!task rm x"))
    events = []
    async for e in it:
        events.append(e)
        if isinstance(e, ApprovalRequest):
            asyncio.get_running_loop().call_soon(
                lambda ref=e.ref: asyncio.ensure_future(b.resolve_approval(ref, False)))
    assert "denied" in events[-1].text


async def test_fail():
    with pytest.raises(BackendError):
        await collect(EchoBackend().stream(Turn(1, "s", "!fail")))
