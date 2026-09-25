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


async def test_streaming_edits_and_long_reply_split_with_html():
    class Long(Backend):
        name = "long"

        async def stream(self, turn):
            for i in range(300):
                yield TextDelta(f"**line {i}** <x> & some filler text to make it long\n")

    async with harness(backends={"long": Long()}, default_backend="long") as h:
        h.fake.push_message(OWNER, "go")
        await h.pump()
        sent = h.fake.sent(OWNER)
        assert len(sent) >= 5
        assert all(m["parse_mode"] == "HTML" and len(m["text"]) <= 4096 for m in sent)
        assert sent[0]["edits"] > 2  # streamed as it arrived
        assert "<b>line 0</b> &lt;x&gt; &amp;" in sent[0]["text"]
        assert "line 299" in sent[-1]["text"]


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
