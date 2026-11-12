from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
