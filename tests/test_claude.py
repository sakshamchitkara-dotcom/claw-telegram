import pytest
from aiohttp import web
from conftest import serve, write_sse

from claw_telegram.backends.base import BackendError, Image, TextDelta, Turn

anthropic = pytest.importorskip("anthropic")
from claw_telegram.backends.claude import ClaudeBackend  # noqa: E402


def sse_message(texts: list[str], stop_reason: str) -> list[tuple[str, dict]]:
    frames = [("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}})]
    for t in texts:
        frames.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": t}}))
    frames += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
               ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason,
                                                                     "stop_sequence": None},
                                  "usage": {"output_tokens": 3}}),
               ("message_stop", {"type": "message_stop"})]
    return frames


async def run(stop_reason="end_turn", status=200):
    seen = []

    async def messages(request):
        seen.append(await request.json())
        if status != 200:
            return web.json_response({"type": "error", "error": {"type": "invalid_request_error",
                                                                 "message": "nope"}}, status=status)
        return await write_sse(request, sse_message(["Hi ", "there"], stop_reason))

    app = web.Application()
    app.router.add_post("/v1/messages", messages)
    async with serve(app) as url:
        client = anthropic.AsyncAnthropic(api_key="test", base_url=url, max_retries=0)
        b = ClaudeBackend(system_prompt="sys", client=client)
        try:
            events = [e async for e in b.stream(Turn(1, "s", "hello", history=[
                {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
                images=[Image("image/png", b"png")]))]
        finally:
            await b.close()
    return seen, events


async def test_streams_text_with_expected_request():
    seen, events = await run()
    assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hi there"
    body = seen[0]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "medium"}
    assert "thinking" not in body and "temperature" not in body
    assert body["system"] == "sys"
    assert body["messages"][-1]["content"][0]["type"] == "image"


async def test_refusal_is_reported():
    _, events = await run(stop_reason="refusal")
    assert "declined" in events[-1].text


async def test_api_error_maps_to_backend_error():
    with pytest.raises(BackendError, match="HTTP 400"):
        await run(status=400)
