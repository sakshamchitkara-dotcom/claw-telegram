from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from claw_telegram.schedule import Cron, next_run, parse_duration, parse_when, split_spec

TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 9, 25, 10, 17, 30, tzinfo=TZ)  # a Friday


def test_durations():
    assert parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration("10") is None and parse_duration("5x") is None and parse_duration("") is None


def test_parse_when():
    assert parse_when("10m", NOW) == NOW + timedelta(minutes=10)
    assert parse_when("11:00", NOW) == datetime(2026, 9, 25, 11, 0, tzinfo=TZ)
    assert parse_when("09:00", NOW) == datetime(2026, 9, 26, 9, 0, tzinfo=TZ)  # already passed today
    assert parse_when("2026-10-01T09:30", NOW) == datetime(2026, 10, 1, 9, 30, tzinfo=TZ)
    for bad in ["2020-01-01T00:00", "25:00", "soon", "0m"]:
        with pytest.raises(ValueError):
            parse_when(bad, NOW)


def test_cron_basics():
    assert Cron("*/15 * * * *").next_after(NOW) == datetime(2026, 9, 25, 10, 30, tzinfo=TZ)
    assert Cron("0 9 * * mon-fri").next_after(NOW) == datetime(2026, 9, 28, 9, 0, tzinfo=TZ)  # skips weekend
    assert Cron("@daily").next_after(NOW) == datetime(2026, 9, 26, 0, 0, tzinfo=TZ)
    assert Cron("0 0 1 jan *").next_after(NOW) == datetime(2027, 1, 1, 0, 0, tzinfo=TZ)
    assert Cron("30 8 29 2 *").next_after(NOW) == datetime(2028, 2, 29, 8, 30, tzinfo=TZ)
    assert Cron("0 12 * * 7").next_after(NOW) == datetime(2026, 9, 27, 12, 0, tzinfo=TZ)  # 7 = Sunday


def test_cron_dom_and_dow_are_ored_when_both_set():
    # 1st of the month OR any Monday -> Monday 28 Sep comes first
    assert Cron("0 8 1 * 1").next_after(NOW) == datetime(2026, 9, 28, 8, 0, tzinfo=TZ)


def test_cron_rejects_bad_expressions():
    for bad in ["* * * *", "60 * * * *", "0 0 31 2 *", "*/0 * * * *", "0 0 * * fun"]:
        with pytest.raises(ValueError):
            Cron(bad).next_after(NOW)


def test_split_spec_and_next_run():
    assert split_spec("0 9 * * 1-5 morning brief please") == ("0 9 * * 1-5", "morning brief please")
    assert split_spec("@hourly check mail") == ("@hourly", "check mail")
    assert split_spec("2h stretch") == ("2h", "stretch")
    assert next_run("2h", NOW) == NOW + timedelta(hours=2)
    with pytest.raises(ValueError):
        next_run("30s", NOW)


def test_local_zone_follows_the_host_zone_name(tmp_path):
    from claw_telegram.schedule import local_zone

    assert local_zone("Asia/Kolkata") == ZoneInfo("Asia/Kolkata")
    link = tmp_path / "localtime"
    link.symlink_to(tmp_path / "zoneinfo/Europe/Berlin")  # only the name matters
    zone = local_zone(localtime=str(link), timezone_file=str(tmp_path / "missing"))
    assert zone == TZ  # a real zone, so summer and winter get different offsets
    assert datetime(2026, 7, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=2)
    assert datetime(2026, 12, 1, 12, tzinfo=zone).utcoffset() == timedelta(hours=1)
    (tmp_path / "timezone").write_text("America/New_York\n")  # Debian style
    assert local_zone(localtime=str(tmp_path / "nope"), timezone_file=str(tmp_path / "timezone")) == ZoneInfo(
        "America/New_York")
    fixed = local_zone(localtime=str(tmp_path / "nope"), timezone_file=str(tmp_path / "missing"))
    assert fixed.utcoffset(None) is not None  # last resort: today's fixed offset
    from zoneinfo import ZoneInfoNotFoundError
    with pytest.raises(ZoneInfoNotFoundError):
        local_zone("Not/AZone")
