from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration, read once from SP_* environment variables or a local .env file.

    Values that contain credentials are SecretStr, so they print as '**********' in logs,
    tracebacks and repr(). Call .get_secret_value() only at the point of use.
    """

    model_config = SettingsConfigDict(env_prefix="SP_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://standingproof:standingproof@localhost:5432/standingproof"
    )
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    environment: Literal["dev", "test", "prod"] = "dev"

    # The provider's own variable name, so the same key works with its other tools.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    draft_model: str = "claude-opus-5-5"
    draft_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    run_max_steps: int = 8
    run_max_cost_usd: float = 0.50
    run_max_seconds: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
