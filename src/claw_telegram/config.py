"""Settings loaded from environment variables (see .env.example)."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
# Telegram's published webhook source ranges (core.telegram.org/bots/webhooks)
TELEGRAM_NETWORKS = ("149.154.160.0/20", "91.108.4.0/22")


def _networks(raw: str) -> tuple[Network, ...]:
    """Comma-separated CIDRs/IPs; the word "telegram" expands to Telegram's ranges. Typos crash at startup."""
    out: list[Network] = []
    for part in raw.split(","):
        part = part.strip()
        if part.lower() == "telegram":
            out.extend(ipaddress.ip_network(n) for n in TELEGRAM_NETWORKS)
        elif part:
            out.append(ipaddress.ip_network(part, strict=False))
    return tuple(out)


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


def _zone_name(timezone: str | None, tz: str | None) -> str:
    """TIMEZONE must be a valid IANA name. TZ is only used when it is one: libc forms such as
    ":/etc/localtime" or "EST5EDT4,M3.2.0" mean "ask the host", which local_zone() does anyway."""
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"TIMEZONE={timezone!r} is not an IANA time zone name (e.g. Europe/Berlin)") from None
        return timezone
    try:
        return tz if tz and ZoneInfo(tz) else ""
    except (ZoneInfoNotFoundError, ValueError):
        return ""


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
    # Groups/supergroups the bot talks in (deny by default). There it only answers
    # messages that mention it, reply to it, or are commands.
    allowed_group_ids: frozenset[int] = field(default_factory=frozenset)
    # Per-role backend allowlist (None = every configured backend) and approval rights.
    role_backends: dict[str, frozenset[str] | None] = field(default_factory=dict)
    role_can_approve: dict[str, bool] = field(
        default_factory=lambda: {"owner": True, "admin": True, "user": False})
    # Agent replies per user per day (in TIMEZONE), by role; 0 or missing = unlimited.
    role_daily_turns: dict[str, int] = field(default_factory=dict)
    mode: str = "polling"  # polling | webhook
    webhook_url: str = ""
    webhook_secret: str = ""
    # Webhook source filtering: empty = any IP. Behind a reverse proxy, list the proxy
    # in webhook_trusted_proxies so the client IP is taken from X-Forwarded-For.
    webhook_ip_allowlist: tuple[Network, ...] = ()
    webhook_trusted_proxies: tuple[Network, ...] = ()
    # Read-only status page at /admin (HTTP Basic auth, any user name). Empty = no page.
    admin_password: str = ""
    http_host: str = "0.0.0.0"
    http_port: int = 8080
    db_path: str = "data/claw-telegram.db"
    default_backend: str = "echo"
    # Failover: tried in order when the chat's backend fails before replying.
    fallback_backends: tuple[str, ...] = ()
    health_interval_s: float = 60  # 0 disables background health probes
    circuit_failures: int = 3
    circuit_cooldown_s: float = 60
    history_limit: int = 40
    rate_limit_per_minute: int = 20
    approval_timeout_s: int = 300
    # A backend that sends nothing for this long fails (and falls over if nothing was shown yet).
    # The clock stops while an approval prompt is open in the chat. 0 = no limit.
    first_token_timeout_s: float = 180
    idle_timeout_s: float = 300
    stream_edit_interval_s: float = 1.2
    long_reply_file_chars: int = 12000  # longer replies arrive as a .md file (0 = never)
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
        if secret and not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", secret):
            # Telegram allows 1-256 of these characters; we also insist on a guess-resistant length.
            raise SystemExit("WEBHOOK_SECRET must be 16-256 characters of A-Z a-z 0-9 _ -")
        admin_password = e.get("ADMIN_PASSWORD", "")
        if admin_password and len(admin_password) < 16:
            raise SystemExit("ADMIN_PASSWORD must be at least 16 characters (e.g. openssl rand -hex 16)")
        return cls(
            telegram_token=token,
            admin_password=admin_password,
            telegram_api_base=e.get("TELEGRAM_API_BASE", cls.telegram_api_base).rstrip("/"),
            owner_ids=_ids(e.get("OWNER_IDS", "")),
            admin_ids=_ids(e.get("ADMIN_IDS", "")),
            allowed_user_ids=_ids(e.get("ALLOWED_USER_IDS", "")),
            allowed_group_ids=_ids(e.get("ALLOWED_GROUP_IDS", "")),
            role_backends={r: _names(e.get(f"ROLE_BACKENDS_{r.upper()}")) for r in ROLES},
            role_can_approve={r: _bool(e.get(f"ROLE_APPROVE_{r.upper()}"), r != "user") for r in ROLES},
            role_daily_turns={r: int(e.get(f"DAILY_TURNS_{r.upper()}") or 0) for r in ROLES},
            mode=mode,
            webhook_url=e.get("WEBHOOK_URL", ""),
            webhook_secret=secret,
            webhook_ip_allowlist=_networks(e.get("WEBHOOK_IP_ALLOWLIST", "")),
            webhook_trusted_proxies=_networks(e.get("WEBHOOK_TRUSTED_PROXIES", "")),
            http_host=e.get("HTTP_HOST", cls.http_host),
            http_port=int(e.get("HTTP_PORT", cls.http_port)),
            db_path=e.get("DB_PATH", cls.db_path),
            default_backend=e.get("DEFAULT_BACKEND", cls.default_backend),
            fallback_backends=tuple(n.strip() for n in e.get("FALLBACK_BACKENDS", "").split(",") if n.strip()),
            health_interval_s=float(e.get("HEALTH_INTERVAL_S", cls.health_interval_s)),
            circuit_failures=int(e.get("CIRCUIT_FAILURES", cls.circuit_failures)),
            circuit_cooldown_s=float(e.get("CIRCUIT_COOLDOWN_S", cls.circuit_cooldown_s)),
            history_limit=int(e.get("HISTORY_LIMIT", cls.history_limit)),
            rate_limit_per_minute=int(e.get("RATE_LIMIT_PER_MINUTE", cls.rate_limit_per_minute)),
            approval_timeout_s=int(e.get("APPROVAL_TIMEOUT_S", cls.approval_timeout_s)),
            first_token_timeout_s=float(e.get("FIRST_TOKEN_TIMEOUT_S", cls.first_token_timeout_s)),
            idle_timeout_s=float(e.get("IDLE_TIMEOUT_S", cls.idle_timeout_s)),
            stream_edit_interval_s=float(e.get("STREAM_EDIT_INTERVAL_S", cls.stream_edit_interval_s)),
            long_reply_file_chars=int(e.get("LONG_REPLY_FILE_CHARS", cls.long_reply_file_chars)),
            timezone=_zone_name(e.get("TIMEZONE"), e.get("TZ")),
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
