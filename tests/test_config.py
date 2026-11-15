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


def test_upload_limits_have_safe_defaults_and_can_be_overridden() -> None:
    defaults = Settings(_env_file=None)
    tighter = Settings(_env_file=None, upload_max_bytes=1000)

    assert defaults.upload_max_bytes == 5 * 1024 * 1024
    assert defaults.upload_max_uncompressed_bytes == 50 * 1024 * 1024
    assert defaults.upload_max_sheets == 20
    assert defaults.upload_max_questions == 5000
    assert defaults.question_max_chars == 4000
    assert tighter.upload_max_bytes == 1000


def test_xml_parsing_is_hardened() -> None:
    import openpyxl.xml

    # openpyxl switches to defusedxml only when it is installed; this fails if it is removed.
    assert openpyxl.xml.DEFUSEDXML is True


def test_declared_check_scopes_are_parsed_from_comma_separated_text() -> None:
    settings = Settings(
        _env_file=None, github_orgs=" Acme, other-org ,,", aws_accounts="123456789012"
    )

    assert settings.declared_scopes("github") == {"acme", "other-org"}
    assert settings.declared_scopes("aws") == {"123456789012"}
    assert Settings(_env_file=None).declared_scopes("github") == set()
    assert settings.declared_scopes("gitlab") == set()
