import pytest
from aiohttp import web
from conftest import chunk, serve, write_sse

from claw_telegram.backends.base import BackendError, Image, Status, TextDelta, Turn
from claw_telegram.backends.openai_compat import OpenAICompatBackend


def make_app(seen: list):
    async def completions(request):
        seen.append((dict(request.headers), await request.json()))
        if request.headers.get("Authorization") != "Bearer k":
            return web.json_response({"error": {"message": "bad key"}}, status=401)
        return await write_sse(request, [
            # Ollama's shape for a reasoning model's thinking phase (observed with qwen3:4b)
            (None, {"choices": [{"delta": {"role": "assistant", "content": "", "reasoning": "We"}}]}),
            (None, chunk("Hel")), (None, chunk("lo")),
            (None, {"choices": [{"delta": {"tool_calls": [{"function": {"name": "search"}}]}}]}),
            (None, "[DONE]"), (None, chunk("ignored after DONE")),
        ])

    async def models(request):
        return web.json_response({"data": [{"id": "hermes3"}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    app.router.add_get("/v1/models", models)
    return app


async def test_streams_deltas_and_sends_history_and_images():
    seen = []
    async with serve(make_app(seen)) as url:
        b = OpenAICompatBackend(f"{url}/v1", "hermes3", api_key="k", system_prompt="sys")
        turn = Turn(1, "s", "what is this", history=[{"role": "user", "content": "hi"},
                                                     {"role": "assistant", "content": "yo"}],
                    images=[Image("image/png", b"\x89PNG")])
        events = [e async for e in b.stream(turn)]
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello"
        assert Status("tool call: search") in events
        assert events[0] == Status("thinking…")  # reasoning isn't shown as text
        body = seen[0][1]
        assert body["stream"] is True and body["model"] == "hermes3"
        assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
        assert body["messages"][-1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert (await b.health()).startswith("ok (1 models, hermes3 present")
        await b.close()


async def test_http_error_becomes_backend_error():
    async with serve(make_app([])) as url:
        b = OpenAICompatBackend(f"{url}/v1", "m", api_key="wrong")
        with pytest.raises(BackendError, match="401: bad key"):
            [e async for e in b.stream(Turn(1, "s", "x"))]
        await b.close()


async def test_unreachable():
    b = OpenAICompatBackend("http://127.0.0.1:9/v1", "m")
    with pytest.raises(BackendError, match="cannot reach"):
        [e async for e in b.stream(Turn(1, "s", "x"))]
    assert (await b.health()).startswith("unreachable")
    await b.close()
