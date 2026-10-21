"""Settings from environment variables (optionally a local .env file).

Secrets are only ever read from the environment. Nothing here is logged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROVIDERS = ("anthropic", "openai_compatible", "mock")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigError(ValueError):
    pass


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is in requirements
        return
    load_dotenv(env_path, override=False)


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], key: str, default: int, minimum: int = 0) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{key} must be >= {minimum}")
    return value


def _float(env: Mapping[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    anthropic_api_key: str | None
    anthropic_model: str
    openai_api_key: str | None
    openai_model: str | None
    openai_base_url: str
    openai_json_mode: str
    llm_effort: str | None
    llm_timeout_seconds: float
    llm_max_tokens: int
    max_input_chars: int
    max_body_bytes: int
    rate_limit_per_minute: int
    daily_request_cap: int
    trusted_proxy_hops: int
    allow_failure_simulation: bool
    log_level: str

    def __repr__(self) -> str:  # never print secrets
        return f"Settings(provider={self.llm_provider!r}, model={self.model_name!r})"

    @property
    def model_name(self) -> str | None:
        if self.llm_provider == "anthropic":
            return self.anthropic_model
        if self.llm_provider == "openai_compatible":
            return self.openai_model
        return "rules-baseline"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        _load_dotenv()
        env = os.environ

    anthropic_key = env.get("ANTHROPIC_API_KEY", "").strip() or None
    openai_key = env.get("OPENAI_API_KEY", "").strip() or None

    provider = env.get("LLM_PROVIDER", "").strip().lower()
    if not provider:
        provider = "anthropic" if anthropic_key else "mock"
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}")

    effort = env.get("LLM_EFFORT", "").strip().lower() or None
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ConfigError(f"LLM_EFFORT must be one of {', '.join(EFFORT_LEVELS)} or empty")

    json_mode = env.get("OPENAI_JSON_MODE", "json_schema").strip().lower() or "json_schema"
    if json_mode not in ("json_schema", "json_object", "none"):
        raise ConfigError("OPENAI_JSON_MODE must be json_schema, json_object or none")

    settings = Settings(
        llm_provider=provider,
        anthropic_api_key=anthropic_key,
        anthropic_model=env.get("ANTHROPIC_MODEL", "").strip() or "claude-haiku-5-5",
        openai_api_key=openai_key,
        openai_model=env.get("OPENAI_MODEL", "").strip() or None,
        openai_base_url=(env.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com/v1").rstrip("/"),
        openai_json_mode=json_mode,
        llm_effort=effort,
        llm_timeout_seconds=_float(env, "LLM_TIMEOUT_SECONDS", 25.0),
        llm_max_tokens=_int(env, "LLM_MAX_TOKENS", 4096, minimum=256),
        max_input_chars=_int(env, "MAX_INPUT_CHARS", 2000, minimum=50),
        max_body_bytes=_int(env, "MAX_BODY_BYTES", 16384, minimum=1024),
        rate_limit_per_minute=_int(env, "RATE_LIMIT_PER_MINUTE", 10, minimum=1),
        daily_request_cap=_int(env, "DAILY_REQUEST_CAP", 500, minimum=1),
        trusted_proxy_hops=_int(env, "TRUSTED_PROXY_HOPS", 0, minimum=0),
        allow_failure_simulation=_bool(env.get("ALLOW_FAILURE_SIMULATION"), True),
        log_level=env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )

    if provider == "anthropic" and not anthropic_key:
        raise ConfigError("LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
    if provider == "openai_compatible" and (not openai_key or not settings.openai_model):
        raise ConfigError("LLM_PROVIDER=openai_compatible needs OPENAI_API_KEY and OPENAI_MODEL")
    return settings
