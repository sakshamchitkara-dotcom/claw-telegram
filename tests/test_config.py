import pytest

from claw_telegram.config import Settings


def test_allowlist_parsing_and_default_deny():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t"})
    assert s.allowed_user_ids == frozenset()
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": " 1, 22;333 "})
    assert s.allowed_user_ids == {1, 22, 333}


def test_bad_allowlist_crashes():
    with pytest.raises(ValueError):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "12,abc"})


def test_webhook_requires_secret():
    with pytest.raises(SystemExit):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "BOT_MODE": "webhook"})


def test_token_required():
    with pytest.raises(SystemExit):
        Settings.from_env({})
