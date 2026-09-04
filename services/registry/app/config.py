"""Configuration, loaded from environment. Fails fast on missing secrets.

There is no default for JWT_SECRET or CREDENTIAL_MASTER_KEY on purpose. A
development default for a signing key is how a development default ends up in
production.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> str:
    """Locate the repository .env regardless of the working directory.

    pydantic-settings resolves a relative env_file against the process working
    directory. That is fine in the container, where the app runs from /srv, but on a
    developer machine the command is just as likely to be issued from
    services/registry, where .env is two levels up. Walking up from this file makes
    the lookup independent of where the command was typed.

    Real environment variables still win over the file, so Docker Compose's
    injected DATABASE_URL is not shadowed by a stale local one.
    """
    for base in Path(__file__).resolve().parents:
        candidate = base / ".env"
        if candidate.is_file():
            return str(candidate)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_find_env_file(), extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # Least-privilege connection used to serve requests (RLS applies).
    database_url: str = Field(alias="DATABASE_URL")
    # Superuser connection used only by migrations and seeding.
    database_admin_url: str = Field(default="", alias="DATABASE_ADMIN_URL")

    jwt_secret: str = Field(alias="JWT_SECRET", min_length=32)
    credential_master_key: str = Field(alias="CREDENTIAL_MASTER_KEY")

    access_token_ttl_minutes: int = Field(default=15, alias="ACCESS_TOKEN_TTL_MINUTES")
    refresh_token_ttl_hours: int = Field(default=12, alias="REFRESH_TOKEN_TTL_HOURS")
    max_failed_logins: int = Field(default=5, alias="MAX_FAILED_LOGINS")
    lockout_minutes: int = Field(default=15, alias="LOCKOUT_MINUTES")

    cors_allowed_origins: str = Field(default="http://localhost:8080", alias="CORS_ALLOWED_ORIGINS")
    rate_limit_per_minute: int = Field(default=120, alias="RATE_LIMIT_PER_MINUTE")

    sandbox_base_url: str = Field(default="", alias="SANDBOX_BASE_URL")
    sandbox_sync_enabled: bool = Field(default=False, alias="SANDBOX_SYNC_ENABLED")

    @field_validator("credential_master_key")
    @classmethod
    def _validate_master_key(cls, v: str) -> str:
        try:
            raw = base64.b64decode(v, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "CREDENTIAL_MASTER_KEY must be base64. Generate with: "
                "python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\""
            ) from exc
        if len(raw) != 32:
            raise ValueError(f"CREDENTIAL_MASTER_KEY must decode to 32 bytes, got {len(raw)}")
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _reject_placeholder(cls, v: str) -> str:
        if "CHANGE_ME" in v:
            raise ValueError("JWT_SECRET is still the placeholder from .env.example")
        return v

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.credential_master_key)

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
