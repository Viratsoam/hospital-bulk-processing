"""Centralized application configuration.

All tunables are read from environment variables (or a local ``.env`` file)
exactly once at import time via :func:`get_settings`, which caches the result.
Nothing else in the codebase reads ``os.environ`` directly, so the effective
configuration of a running process can always be traced back to this module.
"""

from functools import lru_cache
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated view over the process environment.

    Environment variable names are the field names upper-cased with the
    ``HBP_`` prefix, e.g. ``HBP_MAX_HOSPITALS_PER_BATCH``.
    """

    model_config = SettingsConfigDict(
        env_prefix="HBP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Application metadata -------------------------------------------------
    app_name: str = "Hospital Bulk Processing"
    app_version: str = "0.1.0"
    environment: str = "local"
    log_level: str = "INFO"

    # --- Upstream Hospital Directory API --------------------------------------
    # Defaults reflect the deployed staging API verified during integration
    # analysis. Overridable so tests and local runs never touch production data.
    hospital_api_base_url: str = "https://hospital-directory.onrender.com"
    hospital_api_timeout_seconds: float = Field(default=30.0, gt=0)
    hospital_api_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    # Upstream create calls cost ~5.3s each but do not degrade under load, so
    # fan-out width is bounded only to stay a polite client.
    hospital_api_max_concurrency: int = Field(default=20, ge=1, le=100)
    hospital_api_max_retries: int = Field(default=2, ge=0, le=5)

    # --- CSV ingestion limits -------------------------------------------------
    max_hospitals_per_batch: int = Field(default=20, ge=1, le=1000)
    max_csv_size_bytes: int = Field(default=1_000_000, ge=1)

    # --- CORS -----------------------------------------------------------------
    cors_allow_origins: List[str] = Field(default_factory=lambda: ["*"])

    @field_validator("hospital_api_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        """Normalize the base URL so path joining is unambiguous."""
        return value.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _uppercase_log_level(cls, value: str) -> str:
        """Accept ``info`` and ``INFO`` interchangeably."""
        return value.upper()

    @property
    def is_production(self) -> bool:
        """Whether the app is running in a production environment."""
        return self.environment.lower() in {"prod", "production"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so environment parsing happens once; tests that need different
    values should clear the cache via ``get_settings.cache_clear()``.
    """
    return Settings()


def settings_or_none() -> Optional[Settings]:
    """Return settings if they load cleanly, else ``None``.

    Useful for diagnostics paths that must not raise on bad configuration.
    """
    try:
        return get_settings()
    except Exception:  # pragma: no cover - defensive only
        return None
