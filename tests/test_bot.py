from harness import OWNER, STRANGER, harness

from claw_telegram.backends.base import Backend, TextDelta


async def test_stranger_is_denied_and_told_their_id():
    async with harness() as h:
        h.fake.push_message(STRANGER, "hello")
        await h.pump()
        assert h.fake.texts(STRANGER) == [
            f"Not authorized. Your Telegram user id is {STRANGER}; the bot owner must add it to ALLOWED_USER_IDS."]
        assert h.bot.store.count_messages(STRANGER) == 0


async def test_empty_allowlist_denies_everyone():
    async with harness(allowed_user_ids=frozenset()) as h:
        h.fake.push_message(OWNER, "/status")
        await h.pump()
        assert h.fake.texts(OWNER)[0].startswith("Not authorized")


async def test_group_messages_from_strangers_are_silently_ignored():
    async with harness() as h:
        h.fake.push_message(STRANGER, "hi", chat_id=-100)
        await h.pump()
        assert h.fake.texts(-100) == []


async def test_echo_reply_is_stored_and_remembered():
    async with harness() as h:
        h.fake.push_message(OWNER, "first")
        await h.pump()
        h.fake.push_message(OWNER, "second")
        await h.pump()
        texts = h.fake.texts(OWNER)
        assert texts[0].startswith("echo: first") and "(history: 0 msgs)" in texts[0]
        assert "(history: 2 msgs)" in texts[1]
        assert h.bot.store.count_messages(OWNER) == 4


async def test_reset_clears_memory():
    async with harness() as h:
        h.fake.push_message(OWNER, "remember me")
        await h.pump()
        h.fake.push_message(OWNER, "/reset")
        await h.pump()
        h.fake.push_message(OWNER, "again")
        await h.pump()
        assert "(history: 0 msgs)" in h.fake.texts(OWNER)[-1]
        assert h.bot.store.session_id(OWNER).endswith("-1")


async def test_backend_command_lists_and_switches():
    class Other(Backend):
        name = "other"

        async def stream(self, turn):
            yield TextDelta("from other")

    from claw_telegram.backends.echo import EchoBackend
    async with harness(backends={"echo": EchoBackend(), "other": Other()}) as h:
        for t in ["/backend", "/backend nope", "/backend other", "hi"]:
            h.fake.push_message(OWNER, t)
            await h.pump()
        texts = h.fake.texts(OWNER)
        assert "• echo" in texts[0] and "other" in texts[0]
        assert texts[1].startswith("Unknown backend")
        assert texts[2] == "Switched to other."
        assert texts[3] == "from other"


async def test_status_help_start_and_unknown_command():
    async with harness() as h:
        for t in ["/start", "/help", "/status@claw_test_bot", "/nope", "/tasks"]:
            h.fake.push_message(OWNER, t)
            await h.pump()
        start, help_, status, unknown, tasks = h.fake.texts(OWNER)
        assert "echo" in start
        assert "/reset" in help_ and "/tasks" in help_
        assert status.startswith("backend: echo (ok)") and "session: telegram-1001-0" in status
        assert unknown.startswith("Unknown command /nope")
        assert tasks == "No agent tasks yet."


async def test_backend_error_is_shown_and_not_stored():
    async with harness() as h:
        h.fake.push_message(OWNER, "!fail")
        await h.pump()
        assert h.fake.texts(OWNER) == ["⚠️ mock failure requested"]
        assert h.bot.store.count_messages(OWNER) == 0


async def test_rate_limit():
    async with harness(rate_limit_per_minute=2) as h:
        for _ in range(3):
            h.fake.push_message(OWNER, "/help")
        await h.pump()
        assert h.fake.texts(OWNER)[-1] == "Rate limit reached, please wait a minute."


async def test_streaming_edits_and_long_reply_paginates_with_show_more():
    class Long(Backend):
        name = "long"

        async def stream(self, turn):
            for i in range(300):
                yield TextDelta(f"**line {i}** <x> & some filler text to make it long\n")

    async with harness(backends={"long": Long()}, default_backend="long", long_reply_file_chars=0) as h:
        h.fake.push_message(OWNER, "go")
        await h.pump()
        (msg,) = h.fake.sent(OWNER)  # one message, paged in place
        assert msg["edits"] > 2  # streamed as it arrived
        assert msg["parse_mode"] == "HTML" and "<b>line 0</b> &lt;x&gt; &amp;" in msg["text"]
        row = msg["reply_markup"]["inline_keyboard"][0]
        total = int(row[0]["text"].split("/")[1])
        assert total >= 5 and [b["text"] for b in row] == [f"1/{total}", "Show more ▶"]
        seen = [msg["text"]]
        for _ in range(total - 1):
            nxt = msg["reply_markup"]["inline_keyboard"][0][-1]["callback_data"]
            h.fake.push_callback(OWNER, dict(msg), nxt)
            await h.pump()
            seen.append(msg["text"])
            assert len(msg["text"]) <= 4096 and msg["parse_mode"] == "HTML"
        assert "line 299" in seen[-1] and len(set(seen)) == total
        assert [b["text"] for b in msg["reply_markup"]["inline_keyboard"][0]] == ["◀ Prev", f"{total}/{total}"]
        h.fake.push_callback(OWNER, dict(msg), "pg:1:0")  # back to the start
        h.fake.push_callback(OWNER, dict(msg, chat={"id": 555}), "pg:1:1")  # pages of another chat
        await h.pump()
        assert msg["text"] == seen[0]
        answers = [p.get("text") for m, p in h.fake.calls if m == "answerCallbackQuery"]
        assert answers[-1] == "These pages have expired."


async def test_busy_chat_rejects_concurrent_message():
    import asyncio

    gate = asyncio.Event()

    class Slow(Backend):
        name = "slow"

        async def stream(self, turn):
            await gate.wait()
            yield TextDelta("done")

    async with harness(backends={"slow": Slow()}, default_backend="slow") as h:
        h.fake.push_message(OWNER, "one")
        h.fake.push_message(OWNER, "two")
        await h.pump(drain=False)
        gate.set()
        await h.bot.drain()
        texts = h.fake.texts(OWNER)
        assert "Still working on your previous message, one moment." in texts
        assert "done" in texts


async def test_very_long_reply_arrives_as_markdown_file():
    class Huge(Backend):
        name = "huge"

        async def stream(self, turn):
            yield TextDelta("# Report\n\n" + "".join(f"- item {i} <x>\n" for i in range(2000)))

    async with harness(backends={"huge": Huge()}, default_backend="huge", timezone="UTC") as h:
        h.fake.push_message(OWNER, "go", thread=None)
        await h.pump()
        (preview,) = h.fake.sent(OWNER)
        assert preview["parse_mode"] == "HTML" and preview["text"].startswith("<b>Report</b>")
        assert "📄 Full reply (" in preview["text"] and "item 1999" not in preview["text"]
        (doc,) = h.fake.documents
        assert doc["document"]["file_name"].startswith("reply-") and doc["document"]["file_name"].endswith(".md")
        assert doc["data"].decode().endswith("- item 1999 <x>")  # raw Markdown, not HTML
