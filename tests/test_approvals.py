import asyncio

from harness import OWNER, STRANGER, harness

from claw_telegram.backends.base import ApprovalRequest


async def start_task(h, action="rm -rf /tmp/demo"):
    h.fake.push_message(OWNER, f"!task {action}")
    await h.pump(drain=False)
    for _ in range(100):  # let the background turn reach the approval prompt
        if h.fake.with_keyboard(OWNER):
            return h.fake.with_keyboard(OWNER)[0]
        await asyncio.sleep(0.01)
    raise AssertionError("no approval prompt")


def buttons(msg):
    return [b["callback_data"] for b in msg["reply_markup"]["inline_keyboard"][0]]


async def test_approve_runs_action():
    async with harness() as h:
        prompt = await start_task(h)
        assert "rm -rf /tmp/demo" in prompt["text"]
        assert buttons(prompt) == ["ap:1:1", "ap:1:0"]
        h.fake.push_callback(OWNER, prompt, "ap:1:1")
        await h.pump()
        assert "Executed `rm -rf /tmp/demo` (mock)." in "\n".join(h.fake.texts(OWNER)).replace("<code>", "`").replace(
            "</code>", "`")
        assert "✅ Approved by u" in prompt["text"] and "reply_markup" not in prompt
        assert h.bot.store.get_task(1).status == "approved"


async def test_deny_and_double_click():
    async with harness() as h:
        prompt = await start_task(h)
        h.fake.push_callback(OWNER, dict(prompt), "ap:1:0")
        h.fake.push_callback(OWNER, dict(prompt), "ap:1:1")  # late second click
        await h.pump()
        assert "denied" in h.fake.texts(OWNER)[0]  # the reply lands in the placeholder
        answers = [p["text"] for m, p in h.fake.calls if m == "answerCallbackQuery"]
        assert answers[0].startswith("❌ Denied") and answers[1] == "Already resolved."
        assert h.bot.store.get_task(1).status == "denied"


async def test_stranger_cannot_approve():
    async with harness() as h:
        prompt = await start_task(h)
        h.fake.push_callback(STRANGER, prompt, "ap:1:1")
        await h.pump(drain=False)
        assert h.bot.store.get_task(1).status == "pending"
        answers = [p["text"] for m, p in h.fake.calls if m == "answerCallbackQuery"]
        assert answers == ["Not authorized."]
        h.fake.push_callback(OWNER, prompt, "ap:1:0")
        await h.pump()


async def test_approval_for_another_chat_is_rejected():
    async with harness(allowed_user_ids=frozenset({OWNER, 2002})) as h:
        prompt = await start_task(h)
        forged = dict(prompt, chat={"id": 2002})
        h.fake.push_callback(2002, forged, "ap:1:1")
        await h.pump(drain=False)
        assert h.bot.store.get_task(1).status == "pending"
        h.fake.push_callback(OWNER, prompt, "ap:1:0")
        await h.pump()


async def test_unanswered_approval_expires_as_denied():
    async with harness(approval_timeout_s=0) as h:
        h.fake.push_message(OWNER, "!task rm -rf /")
        await h.pump()
        assert h.bot.store.get_task(1).status == "expired"
        reply, prompt = h.fake.texts(OWNER)
        assert "denied" in reply
        assert prompt.endswith("No answer in 0s: denied.")


async def test_tasks_command_lists_history():
    async with harness() as h:
        prompt = await start_task(h, "echo hi")
        h.fake.push_callback(OWNER, prompt, "ap:1:1")
        await h.pump()
        h.fake.push_message(OWNER, "/tasks")
        await h.pump()
        assert h.fake.texts(OWNER)[-1] == "#1 [approved] echo: run shell: echo hi"


async def test_out_of_band_approval_goes_to_owner():
    class OOB:
        approval_sink = None
        resolved = []

        async def resolve_approval(self, ref, approve):
            self.resolved.append((ref, approve))

    from claw_telegram.backends.echo import EchoBackend
    oob = OOB()
    async with harness(backends={"echo": EchoBackend(), "openclaw": oob}) as h:
        await oob.approval_sink(ApprovalRequest("ap-9", "exec: git push"))
        prompt = h.fake.with_keyboard(OWNER)[0]
        assert "exec: git push" in prompt["text"]
        h.fake.push_callback(OWNER, prompt, buttons(prompt)[0])
        await h.pump()
        assert oob.resolved == [("ap-9", True)]
