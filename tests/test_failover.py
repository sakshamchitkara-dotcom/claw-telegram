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
