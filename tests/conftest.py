import json
from contextlib import asynccontextmanager

from aiohttp import web
from aiohttp.test_utils import TestServer


@asynccontextmanager
async def serve(app: web.Application):
    server = TestServer(app)
    await server.start_server()
    try:
        yield str(server.make_url("")).rstrip("/")
    finally:
        await server.close()


async def write_sse(request, frames: list[tuple[str | None, object]]) -> web.StreamResponse:
    """frames: (event_name or None, payload dict or raw str)."""
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)
    await resp.write(b": keepalive\n\n")
    for event, payload in frames:
        data = payload if isinstance(payload, str) else json.dumps(payload)
        prefix = f"event: {event}\n" if event else ""
        await resp.write(f"{prefix}data: {data}\n\n".encode())
    return resp


def chunk(text: str) -> dict:
    return {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}}]}
