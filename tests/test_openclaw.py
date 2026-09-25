import asyncio

from aiohttp import web
from conftest import chunk, serve, write_sse

from claw_telegram.backends.base import ApprovalRequest, Image, TextDelta, Turn
from claw_telegram.backends.openclaw import OpenClawBackend


async def test_chat_sends_only_latest_turn_with_stable_user():
    seen = []

    async def completions(request):
        seen.append((request.headers.get("Authorization"), await request.json()))
        return await write_sse(request, [(None, chunk("done")), (None, "[DONE]")])

    app = web.Application()
    app.router.add_post("/v1/chat/completions", completions)
    async with serve(app) as url:
        b = OpenClawBackend(url, "gw-token", agent="openclaw/main")
        turn = Turn(7, "telegram-7-0", "summarize my day", history=[{"role": "user", "content": "old"}],
                    images=[Image("image/jpeg", b"x")])
        assert [e async for e in b.stream(turn)] == [TextDelta("done")]
        await b.close()
    auth, body = seen[0]
    assert auth == "Bearer gw-token"
    assert body["model"] == "openclaw/main"
    assert body["user"] == "telegram-7-0"
    assert [m["role"] for m in body["messages"]] == ["user"]  # server holds the session
    assert body["messages"][0]["content"][1]["type"] == "image_url"


async def test_exec_approval_over_websocket():
    frames_in: list[dict] = []
    resolved = asyncio.Event()

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "event", "event": "connect.challenge", "payload": {"nonce": "n", "ts": 1}})
        connect = await ws.receive_json()
        frames_in.append(connect)
        await ws.send_json({"type": "res", "id": connect["id"], "ok": True,
                            "payload": {"type": "hello-ok", "protocol": 4}})
        await ws.send_json({"type": "event", "event": "exec.approval.requested",
                            "payload": {"id": "ap-1", "createdAtMs": 1, "expiresAtMs": 2,
                                        "request": {"command": "git push --force", "host": "gateway",
                                                    "sessionKey": "agent:main:x"}}})
        resolve = await ws.receive_json()
        frames_in.append(resolve)
        await ws.send_json({"type": "res", "id": resolve["id"], "ok": True, "payload": {"ok": True}})
        resolved.set()
        await ws.receive()  # wait for client close
        return ws

    app = web.Application()
    app.router.add_get("/", ws_handler)
    async with serve(app) as url:
        b = OpenClawBackend(url, "gw-token", approvals_ws=True)
        got: list[ApprovalRequest] = []

        async def sink(req):
            got.append(req)
            asyncio.create_task(b.resolve_approval(req.ref, False))

        b.approval_sink = sink
        b.start()
        await asyncio.wait_for(resolved.wait(), 5)
        await b.close()

    connect, resolve = frames_in
    p = connect["params"]
    assert connect["method"] == "connect" and p["minProtocol"] == 4 and p["role"] == "operator"
    assert p["client"]["id"] == "gateway-client" and p["client"]["mode"] == "backend"
    assert "operator.approvals" in p["scopes"] and p["auth"] == {"token": "gw-token"}
    assert p["caps"] == ["exec-approvals"]  # without it the gateway never sends the event
    assert got == [ApprovalRequest(ref="ap-1", summary="exec on gateway: git push --force")]
    assert resolve["method"] == "exec.approval.resolve"
    assert resolve["params"] == {"id": "ap-1", "decision": "deny"}
