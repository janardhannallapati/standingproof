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

    # Ceilings for an uploaded questionnaire. Each is checked before the work it bounds.
    upload_max_bytes: int = 5 * 1024 * 1024
    upload_max_uncompressed_bytes: int = 50 * 1024 * 1024
    upload_max_sheets: int = 20
    upload_max_rows_per_sheet: int = 100_000
    upload_max_questions: int = 5_000
    question_max_chars: int = 4_000

    # Which model turns text into vectors. The default runs locally and needs no key.
    # A hosted model is chosen by name and needs its provider's key.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Where the local model file is kept. Unset: the library's own cache directory.
    embedding_cache_dir: str | None = None
    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")

    # Ceilings for the library.
    policy_max_bytes: int = 1024 * 1024
    policy_max_chunks: int = 500
    chunk_max_chars: int = 1_200
    library_text_max_chars: int = 4_000
    search_max_results: int = 20

    # Read-only credentials for live checks. AWS credentials are read by the AWS SDK from
    # its usual places (environment variables, a role); only the region is set here.
    github_token: SecretStr | None = None
    github_api_url: str = "https://api.github.com"
    aws_region: str = "us-east-1"
    # A check that takes longer than this is recorded as unavailable.
    check_timeout_seconds: float = 15.0
    # An observation younger than this is returned again, with no new call.
    evidence_max_age_seconds: int = 900

    # Until sign-in exists, every request acts as this one tenant.
    dev_tenant_name: str = "default"


@lru_cache
def get_settings() -> Settings:
    return Settings()
