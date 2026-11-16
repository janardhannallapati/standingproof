from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    # Which organisations and accounts those credentials may be used on, comma-separated.
    # The credentials belong to the deployment, so this is the operator's decision: a tenant
    # can connect only a scope listed here. Empty means no scope can be connected.
    github_orgs: str = ""
    aws_accounts: str = ""
    github_api_url: str = "https://api.github.com"
    aws_region: str = "us-east-1"
    # A check that takes longer than this is recorded as unavailable.
    check_timeout_seconds: float = 15.0
    # An observation younger than this is returned again, with no new call.
    evidence_max_age_seconds: int = 900

    # Running a questionnaire. A run stops starting questions once it has spent its ceiling.
    run_cost_limit_usd: float = 5.0
    # How many questions one worker works on at once (the provider's rate limit is shared).
    worker_concurrency: int = 4
    # How long a worker may hold a question without renewing. Longer than run_max_seconds,
    # so a healthy worker is never overtaken.
    lease_seconds: int = 180
    # A question is given up as failed after this many attempts.
    question_max_attempts: int = 3
    # The first wait before a question is retried after a provider failure; doubles each time.
    retry_base_seconds: float = 10.0
    # After the sweep queues an open question, how long before it may queue it again.
    requeue_seconds: float = 60.0
    # How often a worker looks for open questions that have no job on the queue.
    sweep_seconds: float = 15.0
    # On SIGTERM, how long questions in progress get to finish their current step.
    shutdown_grace_seconds: float = 30.0
    # Saved workflow state is deleted this long after a question finishes.
    state_keep_days: int = 7
    queue_stream: str = "standingproof:jobs"

    # Review. Until sign-in exists, every decision is recorded under this name.
    dev_reviewer: str = "dev-reviewer"
    # How many times a reviewer may send one question back to the agent.
    max_redrafts: int = 2
    # Web origins whose pages may open a live connection, comma-separated. A WebSocket is
    # not covered by CORS, so this list is the control.
    allowed_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    # A question nobody has decided is closed, and its saved state deleted, after this long.
    review_keep_days: int = 30

    # Until sign-in exists, every request acts as this one tenant.
    dev_tenant_name: str = "default"

    @field_validator("anthropic_api_key", "github_token", "voyage_api_key", mode="before")
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        # Compose passes an unset variable through as "". An empty key is no key: code that
        # asks "is there a credential?" must not go on to send an empty one.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def declared_scopes(self, system: str) -> set[str]:
        """The scopes the operator has declared for a system, lower-cased."""
        text = {"github": self.github_orgs, "aws": self.aws_accounts}.get(system, "")
        return {part.strip().lower() for part in text.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
