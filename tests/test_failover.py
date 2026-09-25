from claw_telegram.backends.base import Backend
from claw_telegram.failover import Breaker, Health, healthy


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def test_breaker_opens_after_threshold_and_half_opens_after_cooldown():
    clock = Clock()
    b = Breaker(threshold=2, cooldown=30, clock=clock)
    b.failure("x")
    assert b.state == "closed" and b.available()
    b.failure("y")
    assert b.state == "open" and not b.available() and b.retry_in() == 30
    clock.t += 30
    assert b.state == "half-open" and b.available()
    b.failure("trial failed")  # one failed trial re-opens
    assert b.state == "open"
    clock.t += 30
    b.success()
    assert b.state == "closed" and b.failures == 0


class Probe(Backend):
    name = "probe"

    def __init__(self, status):
        self.value = status

    async def stream(self, turn):
        yield

    async def health(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


async def test_health_probe_trips_and_recovers():
    clock = Clock()
    backend = Probe("unreachable (ClientConnectorError)")
    h = Health({"p": backend}, threshold=3, cooldown=60, clock=clock)
    assert await h.check("p") == "unreachable (ClientConnectorError)"
    assert h.breakers["p"].state == "open"  # a failed probe trips immediately
    assert "[open, retry in 60s, 1 failure]" in h.describe("p")
    backend.value = "ok (2 models)"
    await h.check("p")
    assert h.breakers["p"].state == "half-open"  # reachable again: next turn is a trial
    backend.value = RuntimeError("boom")
    assert await h.check("p") == "error (RuntimeError)"


def test_healthy_prefixes():
    assert healthy("ok") and healthy("live; models: ok (1 models)")
    assert not healthy("unreachable") and not healthy("error HTTP 500") and not healthy("timeout")


# ---- failover inside the bot ---------------------------------------------------

from harness import OWNER, harness  # noqa: E402

from claw_telegram.backends.base import BackendError, TextDelta  # noqa: E402
from claw_telegram.backends.echo import EchoBackend  # noqa: E402


class Down(Backend):
    name = "down"

    def __init__(self):
        self.calls = 0

    async def stream(self, turn):
        self.calls += 1
        raise BackendError("down: cannot reach http://127.0.0.1:9 (ClientConnectorError)")
        yield

    async def health(self):
        return "unreachable (ClientConnectorError)"


class Flaky(Backend):
    name = "flaky"

    async def stream(self, turn):
        yield TextDelta("partial answer")
        raise BackendError("flaky: connection reset")


async def test_falls_back_in_order_and_opens_the_circuit():
    down = Down()
    backends = {"down": down, "down2": Down(), "echo": EchoBackend()}
    async with harness(backends=backends, default_backend="down", fallback_backends=("down2", "echo"),
                       circuit_failures=2) as h:
        for text in ["one", "two", "three"]:
            h.fake.push_message(OWNER, text)
            await h.pump()
        first = h.fake.sent(OWNER)[0]
        assert first["text"].startswith("echo: one")
        assert first["text"].endswith("<i>↪️ answered by echo; down, down2 failed</i>")
        assert first["edits"] >= 3  # showed "⏳ down failed (...); trying down2" on the way
        assert down.calls == 2  # circuit opened after two failures, third turn skipped it
        assert h.fake.texts(OWNER)[2].startswith("echo: three")
        assert h.bot.health.breakers["down"].state == "open"
        assert "(history: 4 msgs)" in h.fake.texts(OWNER)[2]  # the note isn't stored in history


async def test_no_failover_after_output_was_shown():
    async with harness(backends={"flaky": Flaky(), "echo": EchoBackend()}, default_backend="flaky",
                       fallback_backends=("echo",)) as h:
        h.fake.push_message(OWNER, "hi")
        await h.pump()
        assert h.fake.texts(OWNER) == ["⚠️ flaky: connection reset"]


async def test_all_failed_lists_every_error_and_role_limits_fallbacks():
    backends = {"down": Down(), "down2": Down(), "echo": EchoBackend()}
    async with harness(backends=backends, default_backend="down", fallback_backends=("down2", "echo"),
                       owner_ids=frozenset({1}), allowed_user_ids=frozenset({OWNER}),
                       role_backends={"user": frozenset({"down", "down2"})}) as h:
        h.fake.push_message(OWNER, "hi")
        await h.pump()
        (text,) = h.fake.texts(OWNER)
        assert text.splitlines() == ["⚠️ All backends failed:",
                                     "• down: down: cannot reach http://127.0.0.1:9 (ClientConnectorError)",
                                     "• down2: down: cannot reach http://127.0.0.1:9 (ClientConnectorError)"]


async def test_backend_command_shows_health_circuits_and_order():
    async with harness(backends={"down": Down(), "echo": EchoBackend()}, default_backend="down",
                       fallback_backends=("echo",)) as h:
        h.fake.push_message(OWNER, "/backend")
        await h.pump()
        assert h.fake.texts(OWNER)[0].splitlines() == [
            "Backends (• = this chat):",
            "• down: unreachable (ClientConnectorError) [open, retry in 60s, 1 failure]",
            "  echo: ok [closed]",
            "",
            "Next turn tries: echo",  # down's circuit is open
            "",
            "Switch with /backend <name>.",
        ]


def test_half_open_admits_exactly_one_trial():
    clock = Clock()
    b = Breaker(threshold=1, cooldown=30, clock=clock)
    b.failure("down")
    assert not b.available()
    assert b.begin()  # open, but a last-resort attempt is still allowed
    b.failure("still down")
    clock.t += 30
    assert b.available() and b.begin()  # the trial
    assert not b.available() and not b.begin()  # a concurrent turn is refused
    b.release()  # the trial ended without a verdict
    assert b.begin()
    b.success()
    assert b.state == "closed" and b.begin() and b.begin()


async def test_concurrent_turns_during_half_open_send_one_trial():
    import asyncio

    gate = asyncio.Event()

    class Recovering(Backend):
        name = "rec"

        def __init__(self):
            self.calls = 0

        async def stream(self, turn):
            self.calls += 1
            await gate.wait()
            yield TextDelta("back again")

    rec = Recovering()
    async with harness(backends={"rec": rec, "echo": EchoBackend()}, default_backend="rec",
                       fallback_backends=("echo",), allowed_user_ids=frozenset({OWNER, 2002})) as h:
        breaker = h.bot.health.breakers["rec"]
        breaker.failure("down", trip=True)
        breaker.opened_at -= breaker.cooldown  # cooldown over: half-open
        h.fake.push_message(OWNER, "first")
        h.fake.push_message(2002, "second")
        await h.pump(drain=False)
        await asyncio.sleep(0.05)
        gate.set()
        await h.bot.drain()
        assert rec.calls == 1
        assert h.fake.texts(OWNER) == ["back again"]
        assert h.fake.texts(2002)[0].startswith("echo: second")
        assert breaker.state == "closed"


class Hung(Backend):
    """Accepts the request, then never says anything (or stops after `head`)."""
    name = "hung"

    def __init__(self, head: str = ""):
        self.head = head
        self.cancelled = False

    async def stream(self, turn):
        import asyncio
        if self.head:
            yield TextDelta(self.head)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield


async def test_silent_backend_fails_over_after_first_token_timeout():
    hung = Hung()
    async with harness(backends={"hung": hung, "echo": EchoBackend()}, default_backend="hung",
                       fallback_backends=("echo",), first_token_timeout_s=0.2) as h:
        h.fake.push_message(OWNER, "anyone?")
        await h.pump()
        (text,) = h.fake.texts(OWNER)
        assert text.startswith("echo: anyone?")
        assert text.endswith("<i>↪️ answered by echo; hung failed</i>")
        assert hung.cancelled  # the hung request was torn down, not leaked
        assert h.bot.health.breakers["hung"].failures == 1


async def test_backend_that_stalls_mid_reply_reports_instead_of_retrying():
    async with harness(backends={"hung": Hung(head="half an ans"), "echo": EchoBackend()}, default_backend="hung",
                       fallback_backends=("echo",), idle_timeout_s=0.2) as h:
        h.fake.push_message(OWNER, "go")
        await h.pump()
        assert h.fake.texts(OWNER) == ["⚠️ hung: stalled, nothing new for 0.2s"]


async def test_timeout_clock_stops_while_an_approval_is_open():
    import asyncio
    async with harness(first_token_timeout_s=0.1, idle_timeout_s=0.1) as h:
        h.fake.push_message(OWNER, "!task deploy")
        await h.pump(drain=False)
        await asyncio.sleep(0.4)  # longer than both timeouts
        prompt = h.fake.with_keyboard(OWNER)[0]
        h.fake.push_callback(OWNER, prompt, "ap:1:1")
        await h.pump()
        assert h.fake.texts(OWNER)[0] == "Executed <code>deploy</code> (mock)."
