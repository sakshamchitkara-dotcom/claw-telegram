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


async def test_shutdown_marks_interrupted_replies_and_denies_open_approvals():
    from claw_telegram.backends.echo import EchoBackend

    other = 2002
    async with harness(backends={"slow": Slow(), "echo": EchoBackend()}, default_backend="slow",
                       allowed_user_ids=frozenset({OWNER, other})) as h:
        h.bot.store.set_backend(other, "echo")
        h.fake.push_message(OWNER, "long job")
        h.fake.push_message(other, "!task reboot")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        await h.bot.shutdown(grace=2)
        assert h.fake.texts(OWNER) == ["⚠️ The bot restarted before this reply finished. Please send it again."]
        (prompt,) = [m for m in h.fake.sent(other) if "Approval needed" in m["text"]]
        assert prompt["text"].endswith("⌛ The bot restarted: denied.") and "reply_markup" not in prompt
        assert h.bot.store.get_task(1).status == "expired"
        assert not h.bot._tasks


async def test_approvals_left_pending_by_a_crash_are_denied_at_startup():
    async with harness() as h:
        h.fake.push_message(OWNER, "!task rm -rf /srv")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        prompt = h.fake.with_keyboard(OWNER)[0]
        store = h.bot.store
    # "restart": a new bot on the same database, without a clean shutdown
    async with harness() as h2:
        h2.bot.store = store
        await h2.bot.init()
        assert store.get_task(1).status == "expired"
        assert store.audit_log(1)[0].detail == "pending when the bot restarted"
        h2.fake.messages[(OWNER, prompt["message_id"])] = prompt
        h2.fake.push_callback(OWNER, prompt, "ap:1:1")
        await h2.pump()
        answers = [p["text"] for m, p in h2.fake.calls if m == "answerCallbackQuery"]
        assert answers == ["Already resolved."]
