"""Backend registry: build every backend the environment configures."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import Backend

log = logging.getLogger(__name__)


def build_backends(s: Settings) -> dict[str, Backend]:
    from .echo import EchoBackend

    out: dict[str, Backend] = {"echo": EchoBackend()}
    if s.openclaw_url:
        from .openclaw import OpenClawBackend
        out["openclaw"] = OpenClawBackend(s.openclaw_url, s.openclaw_token, s.openclaw_agent,
                                          approvals_ws=s.openclaw_approvals_ws, system_prompt=s.system_prompt)
    if s.hermes_url:
        from .hermes import HermesBackend
        out["hermes"] = HermesBackend(s.hermes_url, s.hermes_key, s.hermes_model, system_prompt=s.system_prompt)
    if s.openai_url:
        from .openai_compat import OpenAICompatBackend
        out["openai"] = OpenAICompatBackend(s.openai_url, s.openai_model, s.openai_key, system_prompt=s.system_prompt)
    if s.anthropic_enabled:
        from .claude import ClaudeBackend
        out["claude"] = ClaudeBackend(s.anthropic_model, system_prompt=s.system_prompt)
    if s.default_backend not in out:
        raise SystemExit(f"DEFAULT_BACKEND={s.default_backend!r} is not configured; available: {sorted(out)}")
    if missing := [n for n in s.fallback_backends if n not in out]:
        raise SystemExit(f"FALLBACK_BACKENDS lists unconfigured backends {missing}; available: {sorted(out)}")
    return out
