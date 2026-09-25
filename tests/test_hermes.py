import asyncio

from aiohttp import web
from conftest import chunk, serve, write_sse

from claw_telegram.backends.base import ApprovalRequest, Status, TextDelta, Turn
from claw_telegram.backends.hermes import HermesBackend


def make_app(state: dict):
    state["decision"] = asyncio.Event()

    async def completions(request):
        state["headers"] = dict(request.headers)
        state["body"] = await request.json()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'event: hermes.tool.progress\ndata: {"tool": "terminal", "emoji": "\\ud83d\\udcbb", '
                         b'"label": "terminal: rm -rf build", "status": "running"}\n\n')
        await resp.write(b'event: approval.request\ndata: {"event": "approval.request", "run_id": "chatcmpl-1", '
                         b'"request_id": "r1", "command": "rm -rf build", "description": "recursive delete", '
                         b'"choices": ["once", "session", "always", "deny"]}\n\n')
        await state["decision"].wait()
        text = "deleted" if state["choice"] == "once" else "BLOCKED"
        await resp.write(f'data: {{"choices": [{{"delta": {{"content": "{text}"}}}}]}}\n\ndata: [DONE]\n\n'.encode())
        return resp

    async def approval(request):
        body = await request.json()
        state["approval_path"] = request.path
        state["approval_body"] = body
        state["choice"] = body["choice"]
        state["decision"].set()
        return web.json_response({"object": "hermes.run.approval_response", "choice": body["choice"]})

    async def health(request):
        return web.json_response({"status": "ok"})

    async def models(request):
        return web.json_response({"data": [{"id": "hermes-agent"}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    app.router.add_post("/v1/runs/{run_id}/approval", approval)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    return app


async def run(approve: bool):
    state: dict = {}
    async with serve(make_app(state)) as url:
        b = HermesBackend(url, "secret")
        events = []
        async for e in b.stream(Turn(42, "s", "clean the build dir", history=[{"role": "user", "content": "hi"}])):
            events.append(e)
            if isinstance(e, ApprovalRequest):
                await b.resolve_approval(e.ref, approve)
        health = await b.health()
        await b.close()
    return state, events, health


async def test_approve_flow():
    state, events, health = await run(True)
    assert state["headers"]["Authorization"] == "Bearer secret"
    assert state["headers"]["X-Hermes-Session-Key"] == "telegram:42"
    assert len(state["body"]["messages"]) == 2  # stateless: history + new turn
    assert isinstance(events[0], Status) and "terminal: rm -rf build" in events[0].text
    appr = events[1]
    assert isinstance(appr, ApprovalRequest) and "recursive delete" in appr.summary
    assert state["approval_path"] == "/v1/runs/chatcmpl-1/approval"
    assert state["approval_body"] == {"choice": "once", "request_id": "r1"}
    assert events[-1] == TextDelta("deleted")
    assert health.startswith("live; models: ok")


async def test_deny_flow():
    state, events, _ = await run(False)
    assert state["approval_body"]["choice"] == "deny"
    assert events[-1] == TextDelta("BLOCKED")


async def test_plain_chunks_still_work():
    async def completions(request):
        return await write_sse(request, [(None, chunk("hi")), (None, "[DONE]")])

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    async with serve(app) as url:
        b = HermesBackend(url, "k")
        assert [e async for e in b.stream(Turn(1, "s", "x"))] == [TextDelta("hi")]
        await b.close()
