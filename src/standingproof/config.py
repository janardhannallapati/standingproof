from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """All configuration, read once from SP_* environment variables or a local .env file.

    Values that contain credentials are SecretStr, so they print as '**********' in logs,
    tracebacks and repr(). Call .get_secret_value() only at the point of use.
    """

    model_config = SettingsConfigDict(env_prefix="SP_", env_file=".env", extra="ignore")

    database_url: SecretStr = SecretStr(
        "postgresql+asyncpg://standingproof:standingproof@localhost:5432/standingproof"
    )
    # `database_url` above is the owner of the tables: migrations and operator commands use
    # it, and nothing that serves a request does. The running processes connect as three
    # roles with fewer rights (see SECURITY.md): `app` sees one tenant at a time,
    # `system` finds work across tenants, `state` holds the workflow's saved state.
    # Unset outside production: the same server and database as `database_url`, with the
    # role's name as its password. In production each must be set.
    app_database_url: SecretStr | None = None
    system_database_url: SecretStr | None = None
    state_database_url: SecretStr | None = None
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    environment: Literal["dev", "test", "prod"] = "dev"

    # The provider's own variable name, so the same key works with its other tools.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    draft_model: str = "claude-opus-5-5"
    draft_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # The independent checker: a second model that reads each draft against its sources.
    # Off: drafts that pass the rules in code are ready without a second reader.
    check_enabled: bool = True
    # Not the drafter's model by default: a different reader shares fewer blind spots.
    check_model: str = "claude-sonnet-5-5"
    check_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
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

    # Drift: noticing that a setting changed after an answer rested on it.
    # The secret GitHub signs webhook deliveries with. Unset: the route answers 404.
    github_webhook_secret: SecretStr | None = None
    # A delivery larger than this is refused unread. The events we act on are a few KB.
    webhook_max_bytes: int = 1024 * 1024
    # A setting is looked at for a webhook no more often than this, however many arrive.
    webhook_min_gap_seconds: float = 30.0
    # A delivery that has been dealt with is deleted after this long.
    webhook_keep_days: int = 30
    # How long after a delivery before looking, so the change is visible to a read.
    webhook_settle_seconds: float = 2.0
    # Every setting an answer rests on is looked at again this long after it was last seen,
    recheck_seconds: float = 24 * 60 * 60
    # or this long after a look that could not reach the system.
    recheck_retry_seconds: float = 60 * 60
    # How often the watcher looks for deliveries, due settings and new observations.
    drift_poll_seconds: float = 5.0
    # How many of each the watcher handles in one pass.
    drift_batch: int = 20

    # Signs audit entries (see SECURITY.md, "Audit log"). With it, an entry cannot be forged
    # by someone who can run SQL but cannot read this value. Without it the log still shows
    # that entries were not altered or removed. Set it once and keep it: entries written
    # under one key do not verify under another. At least 32 characters.
    audit_key: SecretStr | None = Field(default=None, min_length=32)

    # The most the model may cost in one day (UTC), for the whole deployment and for any
    # one tenant. Checked before every model call; over it, the call is not made and the
    # question goes to a person. `python -m standingproof.limits` sets other values.
    daily_limit_usd: float = 20.0
    tenant_daily_limit_usd: float = 10.0
    # What one model call is counted as until it returns and its real cost replaces this.
    # Sized to the most one call can cost with the default models (a full 16,000-token
    # reply from the drafting model, and a large prompt), so that a call in flight is
    # never counted as less than it may turn out to be. Raise it with a dearer model.
    call_reserve_usd: float = Field(default=0.40, gt=0)

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

    @field_validator(
        "anthropic_api_key",
        "github_token",
        "voyage_api_key",
        "github_webhook_secret",
        "audit_key",
        "app_database_url",
        "system_database_url",
        "state_database_url",
        mode="before",
    )
    @classmethod
    def _blank_is_absent(cls, value: object) -> object:
        # Compose passes an unset variable through as "". An empty key is no key: code that
        # asks "is there a credential?" must not go on to send an empty one.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def role_url(self, role: Literal["app", "system", "state"]) -> str:
        """The database URL one of the three running roles connects with."""
        explicit: SecretStr | None = getattr(self, f"{role}_database_url")
        if explicit is not None:
            return explicit.get_secret_value()
        if self.environment == "prod":
            # Never the owner's URL instead: as the owner, no row-level rule would apply.
            raise ValueError(f"SP_{role.upper()}_DATABASE_URL must be set in production")
        name = f"standingproof_{role}"
        owner = make_url(self.database_url.get_secret_value())
        return owner.set(username=name, password=name).render_as_string(hide_password=False)

    def declared_scopes(self, system: str) -> set[str]:
        """The scopes the operator has declared for a system, lower-cased."""
        text = {"github": self.github_orgs, "aws": self.aws_accounts}.get(system, "")
        return {part.strip().lower() for part in text.split(",") if part.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
