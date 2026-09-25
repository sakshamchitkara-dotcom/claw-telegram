from harness import OWNER, harness
from test_approvals import start_task

from claw_telegram.backends.base import Backend, TextDelta
from claw_telegram.backends.echo import EchoBackend

ADMIN, USER = 2001, 3001


class Other(Backend):
    name = "other"

    async def stream(self, turn):
        yield TextDelta("from other")


def roles(**kw):
    return dict(owner_ids=frozenset({OWNER}), admin_ids=frozenset({ADMIN}), allowed_user_ids=frozenset({USER})) | kw


async def test_user_role_is_limited_to_its_backends():
    backends = {"echo": EchoBackend(), "other": Other()}
    async with harness(backends=backends, default_backend="other",
                       **roles(role_backends={"user": frozenset({"echo"})})) as h:
        for t in ["hi", "/backend", "/backend other", "/backend echo", "hi"]:
            h.fake.push_message(USER, t)
            await h.pump()
        refused, listing, unknown, switched, reply = h.fake.texts(USER)
        assert refused.startswith("Your role (user) can't use the other backend")
        assert "echo" in listing and "other" not in listing
        assert unknown.startswith("Unknown backend 'other'")
        assert switched == "Switched to echo." and reply.startswith("echo: hi")


async def test_user_cannot_approve_but_admin_can_and_both_are_audited():
    async with harness(**roles()) as h:
        prompt = await start_task(h)
        h.fake.push_callback(USER, dict(prompt), "ap:1:1")
        await h.pump(drain=False)
        assert h.bot.store.get_task(1).status == "pending"
        h.fake.push_callback(ADMIN, prompt, "ap:1:1")
        await h.pump()
        answers = [p["text"] for m, p in h.fake.calls if m == "answerCallbackQuery"]
        assert answers[0] == "Your role (user) can't answer approvals."
        assert answers[1].startswith("✅ Approved")
        log = h.bot.store.audit_log()
        assert [(e.action, e.user_id, e.task_id) for e in log] == [("approve", ADMIN, 1), ("request", None, 1)]
        assert "rm -rf /tmp/demo" in log[0].detail


async def test_expiry_is_audited():
    async with harness(approval_timeout_s=0) as h:
        h.fake.push_message(OWNER, "!task reboot")
        await h.pump()
        assert [e.action for e in h.bot.store.audit_log()] == ["expire", "request"]


async def test_runtime_added_user_is_allowed():
    async with harness(**roles()) as h:
        h.fake.push_message(4001, "hi")
        await h.pump()
        h.bot.store.set_user(4001, "user", added_by=OWNER)
        h.fake.push_message(4001, "hi")
        await h.pump()
        denied, reply = h.fake.texts(4001)
        assert denied.startswith("Not authorized") and reply.startswith("echo: hi")
