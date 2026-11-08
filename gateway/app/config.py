"""Validate local deployment settings before starting the gateway."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Load immutable settings from gateway/.env and TOLLGATE_ environment variables."""

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="TOLLGATE_",
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    app_name: str = Field(default="Tollgate", min_length=1, max_length=64)
    service_role: Literal["standalone", "control", "warden"] = "standalone"
    node_id: str = Field(default="local", pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    control_failure_policy: Literal["fail_open", "fail_closed"] = "fail_open"
    heartbeat_seconds: float = Field(default=5, ge=1, le=30)
    node_stale_seconds: int = Field(default=20, ge=5, le=180)
    expected_wardens: list[str] = ["warden-1", "warden-2"]
    environment: Literal["development", "test", "production"] = "development"
    docs_enabled: bool = True
    development_api_key: SecretStr
    provider_api_key: SecretStr = SecretStr("simulator_disabled_" + "x" * 32)
    provider_base_url: str = "http://127.0.0.1:8001"
    database_path: Path = BASE_DIR / "data" / "tollgate.db"
    max_request_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)
    body_read_timeout_seconds: float = Field(default=10, ge=1, le=120)
    provider_timeout_seconds: float = Field(default=180, ge=1, le=300)
    max_provider_bytes: int = Field(default=4_194_304, ge=4096, le=16_777_216)
    worker_enabled: bool = True
    worker_interval_seconds: float = Field(default=0.5, ge=0.1, le=10)
    recovery_after_seconds: float = Field(default=240, ge=1, le=600)
    simulator_scenario: Literal[
        "normal", "slow", "reject", "malformed", "disconnect", "missing_usage"
    ] = "normal"
    simulator_delay_seconds: float = Field(default=0.1, ge=0, le=180)
    simulator_database_path: Path = (
        BASE_DIR.parent / "simulator" / "data" / "simulator.db"
    )

    mode: Literal["simulator", "live"] = "simulator"
    database_url: SecretStr | None = None
    public_origin: str = "http://127.0.0.1:8000"
    openai_api_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    gemini_api_key: SecretStr = SecretStr("")
    enabled_providers: list[Literal["openai", "anthropic", "gemini"]] = [
        "openai",
        "anthropic",
        "gemini",
    ]
    openai_store_responses: bool = False
    model_catalog_path: Path = BASE_DIR / "models.json"
    price_max_age_days: int = Field(default=30, ge=1, le=90)
    session_hours: int = Field(default=8, ge=1, le=24)
    customer_requests_per_minute: int = Field(default=60, ge=1, le=6000)
    customer_concurrency: int = Field(default=5, ge=1, le=100)
    admin_requests_per_minute: int = Field(default=300, ge=30, le=6000)
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    stripe_live_mode: bool = False
    # Offline continuity (schema 4): signed allowances, standby relay, outage alerts.
    allowance_secret: SecretStr = SecretStr("")
    allowance_max_micro: int = Field(default=5_000_000, ge=1_000, le=100_000_000)
    allowance_max_ttl_minutes: int = Field(default=1440, ge=5, le=10_080)
    allowance_max_active: int = Field(default=3, ge=1, le=20)
    allowance_sync_grace_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    relay_url: str = ""
    relay_mode: bool = False
    # Last-resort direct access: restricted provider keys handed to approved customers' apps.
    emergency_direct_enabled: bool = False
    direct_customers: list[str] = []
    emergency_openai_key: SecretStr = SecretStr("")
    emergency_anthropic_key: SecretStr = SecretStr("")
    emergency_gemini_key: SecretStr = SecretStr("")
    relay_database_path: Path = BASE_DIR / "data" / "relay.db"
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    sms_webhook_url: str = ""
    sms_webhook_token: SecretStr = SecretStr("")
    watch_ready_url: str = ""
    watch_control_url: str = ""
    watch_interval_seconds: float = Field(default=10, ge=1, le=300)
    watch_failures_to_alert: int = Field(default=3, ge=1, le=20)
    watch_state_path: Path = BASE_DIR / "data" / "watchdog.json"

    @field_validator("expected_wardens")
    @classmethod
    def valid_warden_ids(cls, values):
        if (
            len(values) > 20
            or len(values) != len(set(values))
            or any(
                re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value) is None for value in values
            )
        ):
            raise ValueError("Use at most 20 unique, URL-safe warden IDs.")
        return values

    @field_validator("enabled_providers")
    @classmethod
    def provider_selection(cls, value):
        if not value or len(value) != len(set(value)):
            raise ValueError("Select at least one provider without duplicates.")
        return value

    @field_validator("public_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Use a single origin, such as https://tollgate.example.com."
            )
        return value.rstrip("/")

    @field_validator("model_catalog_path")
    @classmethod
    def catalog_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else BASE_DIR / value

    @field_validator("development_api_key", "provider_api_key")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        """Require separate randomly generated, URL-safe keys without unsafe defaults."""
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value.get_secret_value()) is None:
            raise ValueError("Generate a URL-safe key containing 32-128 characters.")
        return value

    @field_validator("provider_base_url")
    @classmethod
    def local_simulator_only(cls, value: str) -> str:
        """Fix this MVP to a configured loopback simulator, never a caller-supplied URL."""
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError(
                "Use a loopback simulator URL such as http://127.0.0.1:8001."
            )
        return value.rstrip("/")

    @field_validator("database_path")
    @classmethod
    def absolute_database_path(cls, value: Path) -> Path:
        """Resolve relative database paths against gateway/, not the terminal directory."""
        return value if value.is_absolute() else BASE_DIR / value

    @model_validator(mode="after")
    def safe_recovery_deadline(self) -> Self:
        """Keep recovery beyond the maximum normal provider request duration."""
        if self.node_stale_seconds < self.heartbeat_seconds * 3:
            raise ValueError(
                "Node expiry must allow at least three heartbeat intervals."
            )
        if self.recovery_after_seconds < self.provider_timeout_seconds + 15:
            raise ValueError(
                "Recovery delay must exceed the provider timeout by at least 15 seconds."
            )
        if self.development_api_key == self.provider_api_key:
            raise ValueError("Admin and simulator keys must be different.")
        if self.environment == "production":
            if not self.relay_mode and (
                not self.database_url
                or not self.database_url.get_secret_value().startswith("postgresql://")
            ):
                raise ValueError("Production requires a PostgreSQL database URL.")
            if not self.public_origin.startswith("https://") or self.mode != "live":
                raise ValueError(
                    "Production requires an HTTPS public origin and live mode."
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read and validate startup configuration only once per process."""
    return Settings()
