from aiohttp import web
from conftest import serve
from harness import OWNER, harness

from claw_telegram.transcribe import Transcriber


async def test_voice_note_is_transcribed_then_answered():
    seen = {}

    async def transcriptions(request):
        form = await request.post()
        seen["model"] = form["model"]
        seen["bytes"] = form["file"].file.read()
        seen["auth"] = request.headers.get("Authorization")
        return web.json_response({"text": " remind me to buy milk "})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", transcriptions)
    async with serve(app) as url, harness() as h:
        h.bot.transcriber = Transcriber(f"{url}/v1", "whisper-large-v3", api_key="tk")
        h.fake.add_file("v1", b"OggS-fake-audio")
        h.fake.push_message(OWNER, voice={"file_id": "v1", "duration": 2, "mime_type": "audio/ogg"})
        await h.pump()
        await h.bot.transcriber.close()
        assert seen == {"model": "whisper-large-v3", "bytes": b"OggS-fake-audio", "auth": "Bearer tk"}
        texts = h.fake.texts(OWNER)
        assert texts[0] == "🎙 remind me to buy milk"
        assert texts[1].startswith("echo: remind me to buy milk")


async def test_voice_without_transcriber_explains_setup():
    async with harness() as h:
        h.fake.push_message(OWNER, voice={"file_id": "v", "duration": 1})
        await h.pump()
        assert h.fake.texts(OWNER) == ["Voice notes need a transcription endpoint (set TRANSCRIBE_URL)."]


async def test_transcription_failure_is_reported():
    async with harness() as h:
        h.bot.transcriber = Transcriber("http://127.0.0.1:9/v1")
        h.fake.add_file("v", b"x")
        h.fake.push_message(OWNER, voice={"file_id": "v", "duration": 1})
        await h.pump()
        await h.bot.transcriber.close()
        assert h.fake.texts(OWNER)[0].startswith("Transcription failed: cannot reach")


async def test_slow_transcription_does_not_block_other_chats_but_keeps_chat_order():
    import asyncio

    gate = asyncio.Event()

    async def slow(audio, name):
        await gate.wait()
        return "hello from a voice note"

    other = 2002
    async with harness(allowed_user_ids=frozenset({OWNER, other})) as h:
        h.bot.transcriber = slow
        h.fake.add_file("v1", b"OggS")
        updates = [h.fake.push_message(OWNER, voice={"file_id": "v1", "duration": 60}),
                   h.fake.push_message(OWNER, "/help"),  # same chat: must wait for the voice note
                   h.fake.push_message(other, "/help")]  # other chat: answered right away
        for u in updates:
            h.bot.submit(u)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if h.fake.texts(other):
                break
        assert h.fake.texts(other)[0].startswith("/start")
        assert h.fake.texts(OWNER) == []
        gate.set()
        await h.bot.drain()
        texts = h.fake.texts(OWNER)
        assert texts[0] == "🎙 hello from a voice note"  # the /help answer came after it
        assert any(t.startswith("/start") for t in texts[1:])
        assert h.bot._chat_tail == {}
