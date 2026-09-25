import asyncio

from harness import OWNER, harness

from claw_telegram.backends.base import Backend, TextDelta


class Slow(Backend):
    name = "slow"

    async def stream(self, turn):
        yield TextDelta("thinking about it")
        await asyncio.Event().wait()
        yield TextDelta("never")


async def test_cancel_stops_the_running_turn_and_stores_nothing():
    async with harness(backends={"slow": Slow()}, default_backend="slow") as h:
        h.fake.push_message(OWNER, "/cancel")
        await h.pump()
        assert h.fake.texts(OWNER) == ["Nothing is running here."]
        h.fake.push_message(OWNER, "write me an essay")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        h.fake.push_message(OWNER, "/cancel")
        await h.pump()
        assert h.fake.texts(OWNER)[1] == "⏹ Cancelled by 1001."
        assert h.bot.store.count_messages(OWNER) == 0
        assert not h.bot._busy and not h.bot._turns
        h.fake.push_message(OWNER, "/status")  # the chat is usable again
        await h.pump()
        assert h.fake.texts(OWNER)[-1].startswith("backend: slow")


async def test_cancel_denies_the_approval_the_turn_was_waiting_for():
    async with harness() as h:
        h.fake.push_message(OWNER, "!task rm -rf /srv")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        prompt = h.fake.with_keyboard(OWNER)[0]
        h.fake.push_message(OWNER, "/cancel")
        await h.pump()
        assert prompt["text"].endswith("⏹ Cancelled: denied.") and "reply_markup" not in prompt
        assert h.bot.store.get_task(1).status == "cancelled"
        assert h.bot.store.audit_log(1)[0].action == "cancel"
        h.fake.push_callback(OWNER, prompt, "ap:1:1")  # a stale tap can't approve it any more
        await h.pump()
        answers = [p["text"] for m, p in h.fake.calls if m == "answerCallbackQuery"]
        assert answers == ["Already resolved."]
