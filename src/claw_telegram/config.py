"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _ids(raw: str) -> frozenset[int]:
    out = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.add(int(part))  # a typo here should crash at startup, not silently open the bot
    return frozenset(out)


def _names(raw: str | None) -> frozenset[str] | None:
    """Comma-separated backend names; empty or "*" means no restriction (None)."""
    names = frozenset(n.strip() for n in (raw or "").split(",") if n.strip())
    return None if not names or "*" in names else names


ROLES = ("owner", "admin", "user")  # highest first


def _bool(raw: str | None, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_api_base: str = "https://api.telegram.org"
    # Deny by default: an empty allowlist means nobody can use the bot.
    # Roles: owner > admin > user. If owner_ids is empty, allowed_user_ids are all
    # owners (the single-tier behaviour of 0.1.x); otherwise they are plain users.
    owner_ids: frozenset[int] = field(default_factory=frozenset)
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    # Per-role backend allowlist (None = every configured backend) and approval rights.
    role_backends: dict[str, frozenset[str] | None] = field(default_factory=dict)
    role_can_approve: dict[str, bool] = field(
        default_factory=lambda: {"owner": True, "admin": True, "user": False})
    mode: str = "polling"  # polling | webhook
    webhook_url: str = ""
    webhook_secret: str = ""
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    db_path: str = "data/claw-telegram.db"
    default_backend: str = "echo"
    history_limit: int = 40
    rate_limit_per_minute: int = 20
    approval_timeout_s: int = 300
    stream_edit_interval_s: float = 1.2
    timezone: str = ""  # IANA name for /remind and /every; empty = the host's local time
    max_schedules_per_user: int = 20
    system_prompt: str = "You are a helpful personal assistant talking to the user over Telegram."
    # OpenClaw gateway
    openclaw_url: str = ""
    openclaw_token: str = ""
    openclaw_agent: str = "openclaw/default"
    openclaw_approvals_ws: bool = False
    # Hermes Agent API server
    hermes_url: str = ""
    hermes_key: str = ""
    hermes_model: str = "hermes-agent"
    # Generic OpenAI-compatible endpoint (e.g. Hermes models on Ollama/vLLM/OpenRouter)
    openai_url: str = ""
    openai_key: str = ""
    openai_model: str = ""
    # Claude fallback
    anthropic_model: str = "claude-opus-5-5"
    anthropic_enabled: bool = False
    # Voice transcription (OpenAI-compatible /v1/audio/transcriptions)
    transcribe_url: str = ""
    transcribe_key: str = ""
    transcribe_model: str = "whisper-1"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        token = e.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise SystemExit("TELEGRAM_BOT_TOKEN is required")
        mode = e.get("BOT_MODE", "polling")
        if mode not in {"polling", "webhook"}:
            raise SystemExit("BOT_MODE must be 'polling' or 'webhook'")
        secret = e.get("WEBHOOK_SECRET", "")
        if mode == "webhook" and not secret:
            raise SystemExit("WEBHOOK_SECRET is required in webhook mode")
        return cls(
            telegram_token=token,
            telegram_api_base=e.get("TELEGRAM_API_BASE", cls.telegram_api_base).rstrip("/"),
            owner_ids=_ids(e.get("OWNER_IDS", "")),
            admin_ids=_ids(e.get("ADMIN_IDS", "")),
            allowed_user_ids=_ids(e.get("ALLOWED_USER_IDS", "")),
            role_backends={r: _names(e.get(f"ROLE_BACKENDS_{r.upper()}")) for r in ROLES},
            role_can_approve={r: _bool(e.get(f"ROLE_APPROVE_{r.upper()}"), r != "user") for r in ROLES},
            mode=mode,
            webhook_url=e.get("WEBHOOK_URL", ""),
            webhook_secret=secret,
            http_host=e.get("HTTP_HOST", cls.http_host),
            http_port=int(e.get("HTTP_PORT", cls.http_port)),
            db_path=e.get("DB_PATH", cls.db_path),
            default_backend=e.get("DEFAULT_BACKEND", cls.default_backend),
            history_limit=int(e.get("HISTORY_LIMIT", cls.history_limit)),
            rate_limit_per_minute=int(e.get("RATE_LIMIT_PER_MINUTE", cls.rate_limit_per_minute)),
            approval_timeout_s=int(e.get("APPROVAL_TIMEOUT_S", cls.approval_timeout_s)),
            stream_edit_interval_s=float(e.get("STREAM_EDIT_INTERVAL_S", cls.stream_edit_interval_s)),
            timezone=e.get("TIMEZONE") or e.get("TZ", ""),
            max_schedules_per_user=int(e.get("MAX_SCHEDULES_PER_USER", cls.max_schedules_per_user)),
            system_prompt=e.get("SYSTEM_PROMPT", cls.system_prompt),
            openclaw_url=e.get("OPENCLAW_URL", "").rstrip("/"),
            openclaw_token=e.get("OPENCLAW_TOKEN", ""),
            openclaw_agent=e.get("OPENCLAW_AGENT", cls.openclaw_agent),
            openclaw_approvals_ws=_bool(e.get("OPENCLAW_APPROVALS_WS")),
            hermes_url=e.get("HERMES_URL", "").rstrip("/"),
            hermes_key=e.get("HERMES_API_KEY", ""),
            hermes_model=e.get("HERMES_MODEL", cls.hermes_model),
            openai_url=e.get("OPENAI_BASE_URL", "").rstrip("/"),
            openai_key=e.get("OPENAI_API_KEY", ""),
            openai_model=e.get("OPENAI_MODEL", ""),
            anthropic_model=e.get("ANTHROPIC_MODEL", cls.anthropic_model),
            anthropic_enabled=_bool(e.get("ENABLE_CLAUDE")),
            transcribe_url=e.get("TRANSCRIBE_URL", "").rstrip("/"),
            transcribe_key=e.get("TRANSCRIBE_API_KEY", ""),
            transcribe_model=e.get("TRANSCRIBE_MODEL", cls.transcribe_model),
        )

    def env_role(self, user_id: int) -> str | None:
        """Role granted by the environment (runtime-added users live in the store)."""
        if user_id in self.owner_ids or (not self.owner_ids and user_id in self.allowed_user_ids):
            return "owner"
        if user_id in self.admin_ids:
            return "admin"
        if user_id in self.allowed_user_ids:
            return "user"
        return None

    @property
    def owners(self) -> frozenset[int]:
        return self.owner_ids or self.allowed_user_ids

    def backend_allowed(self, role: str, backend: str) -> bool:
        allowed = self.role_backends.get(role)
        return allowed is None or backend in allowed

    def can_approve(self, role: str | None) -> bool:
        return bool(role) and self.role_can_approve.get(role, False)
