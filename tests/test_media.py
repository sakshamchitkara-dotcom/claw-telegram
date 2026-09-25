from harness import OWNER, harness

from claw_telegram.backends.base import Backend, TextDelta


class TextOnly(Backend):
    name = "textonly"

    async def stream(self, turn):
        yield TextDelta(f"got {len(turn.text)} chars")


async def test_photo_is_forwarded_as_image():
    async with harness() as h:
        h.fake.add_file("small", b"s" * 10)
        h.fake.add_file("big", b"\xff\xd8" + b"b" * 100)
        h.fake.push_message(OWNER, caption="what is this?", photo=[{"file_id": "small", "width": 90, "height": 90},
                                                                    {"file_id": "big", "width": 800, "height": 800}])
        await h.pump()
        reply = h.fake.texts(OWNER)[0]
        assert "received 1 image(s): image/jpeg 102B" in reply
        assert "echo: what is this?" in reply


async def test_image_rejected_for_text_only_backend():
    async with harness(backends={"textonly": TextOnly()}, default_backend="textonly") as h:
        h.fake.add_file("p", b"x")
        h.fake.push_message(OWNER, photo=[{"file_id": "p", "width": 1, "height": 1}])
        await h.pump()
        assert h.fake.texts(OWNER) == ["The textonly backend does not accept images."]


async def test_text_document_is_inlined():
    async with harness() as h:
        h.fake.add_file("d", b"hello from a file")
        h.fake.push_message(OWNER, caption="summarize",
                            document={"file_id": "d", "file_name": "notes.md", "mime_type": "text/markdown",
                                      "file_size": 17})
        await h.pump()
        stored = h.bot.store.history(OWNER, 5)[0]["content"]
        assert stored == "summarize\n\nAttached file notes.md:\n```\nhello from a file\n```"


async def test_binary_document_is_refused():
    async with harness() as h:
        h.fake.push_message(OWNER, document={"file_id": "z", "file_name": "a.zip", "mime_type": "application/zip"})
        await h.pump()
        assert h.fake.texts(OWNER) == ["Can't read a.zip: only images and text files are supported."]


async def test_missing_file_reports_download_error():
    async with harness() as h:
        h.fake.push_message(OWNER, photo=[{"file_id": "gone", "width": 1, "height": 1}])
        await h.pump()
        assert h.fake.texts(OWNER)[0].startswith("Could not download the attachment")
