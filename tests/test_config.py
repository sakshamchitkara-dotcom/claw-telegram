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


def test_roles_legacy_allowlist_means_owners():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_USER_IDS": "1,2"})
    assert s.env_role(1) == s.env_role(2) == "owner" and s.env_role(3) is None
    assert s.owners == {1, 2}


def test_roles_tiers_backends_and_approval_rights():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "OWNER_IDS": "1", "ADMIN_IDS": "2", "ALLOWED_USER_IDS": "3",
                           "ROLE_BACKENDS_USER": "echo, openai", "ROLE_BACKENDS_ADMIN": "*",
                           "ROLE_APPROVE_ADMIN": "false"})
    assert [s.env_role(i) for i in (1, 2, 3, 4)] == ["owner", "admin", "user", None]
    assert s.backend_allowed("user", "openai") and not s.backend_allowed("user", "hermes")
    assert s.backend_allowed("admin", "hermes") and s.backend_allowed("owner", "anything")
    assert s.can_approve("owner") and not s.can_approve("admin") and not s.can_approve("user")
    assert not s.can_approve(None)


def test_webhook_ip_allowlist_parsing():
    s = Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "WEBHOOK_IP_ALLOWLIST": "telegram, 203.0.113.7"})
    assert [str(n) for n in s.webhook_ip_allowlist] == ["149.154.160.0/20", "91.108.4.0/22", "203.0.113.7/32"]
    with pytest.raises(ValueError):
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "t", "WEBHOOK_IP_ALLOWLIST": "10.0.0.0/33"})
