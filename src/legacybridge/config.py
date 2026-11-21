"""Environment configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

TRUE_VALUES = {"true", "1", "yes"}


def parse_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in TRUE_VALUES


@dataclass(frozen=True)
class BridgeSettings:
    legacy_base_url: str = "http://localhost:8090"
    legacy_login: str = "svc_bridge"
    legacy_secret: str = "halden-demo-secret"
    legacy_timeout_sec: float = 5.0
    legacy_max_retries: int = 2
    llm_model: str = "anthropic:claude-opus-5-5"
    llm_enable: bool = False
    anthropic_api_key_present: bool = False

    @classmethod
    def from_env(cls) -> "BridgeSettings":
        env = os.environ
        return cls(
            legacy_base_url=env.get("LEGACY_BASE_URL") or cls.legacy_base_url,
            legacy_login=env.get("LEGACY_LOGIN") or cls.legacy_login,
            legacy_secret=env.get("LEGACY_SECRET") or cls.legacy_secret,
            legacy_timeout_sec=float(env.get("LEGACY_TIMEOUT_SEC") or cls.legacy_timeout_sec),
            legacy_max_retries=int(env.get("LEGACY_MAX_RETRIES") or cls.legacy_max_retries),
            llm_model=env.get("LLM_MODEL") or cls.llm_model,
            llm_enable=parse_bool(env.get("LLM_ENABLE")),
            anthropic_api_key_present=bool((env.get("ANTHROPIC_API_KEY") or "").strip()),
        )
