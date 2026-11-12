import pytest

from standingproof.config import Settings, get_settings


def test_defaults_point_at_local_services() -> None:
    settings = Settings(_env_file=None)

    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert settings.redis_url.get_secret_value() == "redis://localhost:6379/0"
    assert settings.environment == "dev"


def test_environment_variables_with_prefix_override_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SP_REDIS_URL", "redis://cache:6379/5")
    get_settings.cache_clear()

    assert get_settings().redis_url.get_secret_value() == "redis://cache:6379/5"

    get_settings.cache_clear()


def test_connection_strings_do_not_leak_into_repr() -> None:
    settings = Settings(_env_file=None)

    assert "standingproof:standingproof" not in repr(settings)
