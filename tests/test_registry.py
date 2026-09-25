import pytest

from claw_telegram.backends import build_backends
from claw_telegram.config import Settings


def test_only_configured_backends_are_built():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "OPENAI_BASE_URL": "http://localhost:11434/v1",
                           "OPENAI_MODEL": "hermes3", "HERMES_URL": "http://h:8642", "HERMES_API_KEY": "k"})
    assert sorted(build_backends(s)) == ["echo", "hermes", "openai"]


def test_unknown_default_backend_fails_fast():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "DEFAULT_BACKEND": "openclaw"})
    with pytest.raises(SystemExit):
        build_backends(s)


def test_hermes_requires_key():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "HERMES_URL": "http://h:8642"})
    with pytest.raises(ValueError):
        build_backends(s)
