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
