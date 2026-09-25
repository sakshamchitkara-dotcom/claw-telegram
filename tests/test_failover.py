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
