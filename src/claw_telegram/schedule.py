"""Time parsing for /remind and /every: relative/absolute times and 5-field cron.

No dependencies. Cron supports `*`, lists, ranges, steps, month/weekday names
and the @hourly/@daily/@weekly/@monthly/@yearly aliases, with the usual
rule that day-of-month and day-of-week are OR-ed when both are restricted.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_DURATION = re.compile(r"(\d+)([smhdw])")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@weekly": "0 0 * * 0",
            "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *"}
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
MIN_INTERVAL_S = 60


def local_zone(name: str = "", localtime: str = "/etc/localtime", timezone_file: str = "/etc/timezone") -> tzinfo:
    """TIMEZONE if given, else the host's IANA zone so DST changes are followed without a restart.

    The host zone comes from the /etc/localtime symlink (Linux, macOS, Docker images) or
    /etc/timezone (Debian). Only if neither names a zone do we fall back to today's fixed UTC offset.
    """
    if name:
        return ZoneInfo(name)  # a typo should stop the bot, not silently use another zone
    candidates = []
    try:
        target = os.readlink(localtime)  # e.g. /usr/share/zoneinfo/Europe/Berlin
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    try:
        with open(timezone_file) as f:
            candidates.append(f.read().strip())
    except OSError:
        pass
    for candidate in candidates:
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    fixed = datetime.now().astimezone().tzinfo
    log.warning("can't tell the host's time zone; using a fixed %s. Set TIMEZONE to follow DST.", fixed)
    return fixed


def parse_duration(text: str) -> timedelta | None:
    """'90s', '10m', '1h30m', '2d', '1w' -> timedelta; None if it isn't a duration."""
    text = text.lower()
    if not text or _DURATION.sub("", text):
        return None
    return timedelta(seconds=sum(int(n) * _UNITS[u] for n, u in _DURATION.findall(text)))


def _later(now: datetime, d: timedelta) -> datetime:
    """now + d in real (elapsed) time; plain aware arithmetic is wall-clock and is off by an hour across DST."""
    return (now.astimezone(timezone.utc) + d).astimezone(now.tzinfo)


def parse_when(text: str, now: datetime) -> datetime:
    """A reminder time: a duration from now, HH:MM (next occurrence) or YYYY-MM-DDTHH:MM, in now's timezone."""
    if (d := parse_duration(text)) is not None:
        if not d:
            raise ValueError("duration must be positive")
        return _later(now, d)
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", text):
        h, mi = int(m[1]), int(m[2])
        if h > 23 or mi > 59:
            raise ValueError(f"bad time {text!r}")
        at = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    try:
        at = datetime.fromisoformat(text.replace("_", "T"))
    except ValueError:
        raise ValueError(f"can't parse time {text!r}; use 10m, 2h30m, 14:30 or 2026-10-01T09:00") from None
    at = at.replace(tzinfo=now.tzinfo) if at.tzinfo is None else at
    if at <= now:
        raise ValueError(f"{text} is in the past")
    return at


def _field(text: str, lo: int, hi: int, names: dict[str, int] | None = None) -> frozenset[int]:
    out: set[int] = set()
    for part in text.lower().split(","):
        rng, _, step_s = part.partition("/")
        step = int(step_s) if step_s else 1
        if step < 1:
            raise ValueError(f"bad step in {text!r}")
        if rng == "*":
            a, b = lo, hi
        else:
            ends = [names.get(x, x) if names else x for x in rng.split("-", 1)]
            a = int(ends[0])
            b = int(ends[1]) if len(ends) == 2 else (hi if step_s else a)
        if not lo <= a <= b <= hi:
            raise ValueError(f"{part!r} is outside {lo}-{hi}")
        out.update(range(a, b + 1, step))
    return frozenset(out)


class Cron:
    def __init__(self, expr: str):
        self.expr = expr.strip()
        fields = _ALIASES.get(self.expr.lower(), self.expr).split()
        if len(fields) != 5:
            raise ValueError(f"cron needs 5 fields (minute hour day month weekday), got {expr!r}")
        mi, h, dom, mon, dow = fields
        self.minutes = _field(mi, 0, 59)
        self.hours = _field(h, 0, 23)
        self.days = _field(dom, 1, 31)
        self.months = _field(mon, 1, 12, _MONTHS)
        self.weekdays = frozenset(d % 7 for d in _field(dow, 0, 7, _DAYS))  # 0 and 7 are Sunday
        self._dom_any, self._dow_any = dom == "*", dow == "*"

    def _day_ok(self, t: datetime) -> bool:
        dom_ok = t.day in self.days
        dow_ok = (t.weekday() + 1) % 7 in self.weekdays
        if self._dom_any or self._dow_any:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def next_after(self, after: datetime) -> datetime:
        # Walks local wall-clock time like cron does. A time inside a DST gap maps to just after the
        # jump (02:30 -> 03:30); a time inside the repeated hour fires once, on its first pass.
        tz = after.tzinfo
        t = after.replace(second=0, microsecond=0, tzinfo=None) + timedelta(minutes=1)
        for _ in range(200_000):
            if t.month not in self.months:
                t = (t.replace(day=1) + timedelta(days=32)).replace(day=1, hour=0, minute=0)
            elif not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t += timedelta(minutes=1)
            else:
                at = t.replace(tzinfo=tz)
                if at.timestamp() > after.timestamp():
                    return datetime.fromtimestamp(at.timestamp(), tz)
                t += timedelta(minutes=1)  # already passed: we're in the repeated hour
        raise ValueError(f"cron {self.expr!r} never fires")


def split_spec(arg: str) -> tuple[str, str]:
    """'/every' argument -> (schedule spec, prompt). Spec is a duration, an @alias or 5 cron fields."""
    parts = arg.split(None, 1)
    if parts and (parts[0].startswith("@") or parse_duration(parts[0]) is not None):
        return parts[0], parts[1] if len(parts) > 1 else ""
    parts = arg.split(None, 5)
    return " ".join(parts[:5]), parts[5] if len(parts) > 5 else ""


def next_run(spec: str, after: datetime) -> datetime:
    """Next firing time of a repeating spec (interval or cron) strictly after `after`."""
    if (d := parse_duration(spec)) is not None:
        if d.total_seconds() < MIN_INTERVAL_S:
            raise ValueError(f"interval must be at least {MIN_INTERVAL_S}s")
        return _later(after, d)
    return Cron(spec).next_after(after)
