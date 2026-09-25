"""Backend health checks and per-backend circuit breakers for failover.

A breaker opens after `threshold` consecutive failures (or one failed health
probe) and stays open for `cooldown` seconds. After that it is half-open: exactly
one turn is let through as a trial (`begin()` claims it), and its outcome closes
or re-opens the breaker. A health probe that succeeds while the breaker is open
makes it half-open early.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

from .backends.base import Backend

log = logging.getLogger(__name__)
PROBE_TIMEOUT_S = 8


def healthy(status: str) -> bool:
    """Backends report health as text; these prefixes mean reachable and serving."""
    return status.startswith(("ok", "live"))


class Breaker:
    def __init__(self, threshold: int = 3, cooldown: float = 60, clock=time.monotonic):
        self.threshold = max(1, threshold)
        self.cooldown = cooldown
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.last_error = ""
        self.trial = False  # a half-open trial turn is in flight

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def available(self) -> bool:
        """Would a turn be let through right now? (Doesn't claim the half-open trial.)"""
        state = self.state
        return state == "closed" or (state == "half-open" and not self.trial)

    def begin(self) -> bool:
        """Claim the right to send a turn. In half-open state only the first caller gets it."""
        if self.state == "half-open":
            if self.trial:
                return False
            self.trial = True
        return True  # closed, or open but tried anyway because every backend is down

    def release(self) -> None:
        """The claimed turn ended without a verdict on the backend (e.g. an internal error)."""
        self.trial = False

    def success(self) -> None:
        self.failures, self.opened_at, self.trial = 0, None, False

    def failure(self, error: str, trip: bool = False) -> None:
        self.trial = False
        self.failures += 1
        self.last_error = error
        # a failed half-open trial (opened_at still set) re-opens at once
        if trip or self.failures >= self.threshold or self.opened_at is not None:
            if self.state != "open":
                log.warning("circuit open: %s", error)
            self.opened_at = self.clock()

    def retry_in(self) -> int:
        return max(0, math.ceil(self.cooldown - (self.clock() - self.opened_at))) if self.opened_at is not None else 0


class Health:
    """Breakers plus the latest health probe result for every backend."""

    def __init__(self, backends: dict[str, Backend], threshold: int = 3, cooldown: float = 60,
                 clock=time.monotonic):
        self.backends = backends
        self.breakers = {n: Breaker(threshold, cooldown, clock) for n in backends}
        self.status: dict[str, str] = {}

    async def check(self, name: str) -> str:
        try:
            status = await asyncio.wait_for(self.backends[name].health(), PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            status = "timeout"
        except Exception as e:  # a broken probe is a failed probe
            status = f"error ({e.__class__.__name__})"
        self.status[name] = status
        breaker = self.breakers[name]
        if not healthy(status):
            breaker.failure(f"health: {status}", trip=True)
        elif breaker.state == "open":
            breaker.opened_at -= breaker.cooldown  # reachable again: allow a trial turn now
        return status

    async def check_all(self) -> dict[str, str]:
        names = list(self.backends)
        results = await asyncio.gather(*(self.check(n) for n in names))
        return dict(zip(names, results, strict=True))

    async def run(self, interval: float) -> None:
        while True:
            await self.check_all()
            await asyncio.sleep(interval)

    def describe(self, name: str) -> str:
        b = self.breakers[name]
        state = b.state
        if state == "open":
            state += f", retry in {b.retry_in()}s"
        if b.trial:
            state += ", trial running"
        if b.failures:
            state += f", {b.failures} failure{'s' if b.failures != 1 else ''}"
        return f"{self.status.get(name, 'not checked yet')} [{state}]"
