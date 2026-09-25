from harness import OWNER, harness

from claw_telegram.backends.base import Backend, TextDelta


async def say(h, text):
    h.fake.push_message(OWNER, text)
    await h.pump()
    return h.fake.texts(OWNER)[-1]


async def test_summarize_replaces_a_long_history_with_the_summary():
    async with harness() as h:
        assert await say(h, "/summarize") == "Nothing to summarize yet."
        for t in ["my cat is called Miso", "I live in Lisbon", "remind me about the dentist"]:
            await say(h, t)
        assert h.bot.store.count_messages(OWNER) == 6
        summary = await say(h, "/summarize")
        assert summary.startswith("echo: Summarize our conversation so far")
        assert "(history: 6 msgs)" in summary  # the backend saw the whole conversation
        assert summary.endswith("<i>🗜 This summary replaced 6 stored messages.</i>")
        stored = h.bot.store.all_messages(OWNER)
        assert [m["role"] for m in stored] == ["user", "assistant"] and "🗜" not in stored[1]["content"]
        assert "(history: 2 msgs)" in await say(h, "and now?")  # later turns carry only the summary


async def test_summarize_on_a_stateful_backend_keeps_the_history():
    class Gateway(Backend):
        name = "gw"
        stateful = True  # the transcript lives on the server; we can't swap it

        async def stream(self, turn):
            yield TextDelta("- you have a cat")

    async with harness(backends={"gw": Gateway()}, default_backend="gw") as h:
        await say(h, "my cat is called Miso")
        assert await say(h, "/summarize") == "- you have a cat"
        assert h.bot.store.count_messages(OWNER) == 4
