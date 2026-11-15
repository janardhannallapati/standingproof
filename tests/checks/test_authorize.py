from standingproof.checks.authorize import authorize
from standingproof.checks.registry import CHECKS

GITHUB = [("github", "acme")]
AWS = [("aws", "123456789012")]


def test_an_unknown_check_is_denied() -> None:
    decision = authorize(None, "acme", GITHUB)

    assert decision.allowed is False
    assert decision.reason == "unknown_check"


def test_nothing_connected_means_everything_is_denied() -> None:
    for check in CHECKS.values():
        assert authorize(check, "acme", []).allowed is False


def test_an_org_check_is_allowed_for_a_connected_org_whatever_its_letter_case() -> None:
    check = CHECKS["github.org_two_factor"]

    assert authorize(check, "acme", GITHUB).allowed is True
    assert authorize(check, "ACME", GITHUB).allowed is True
    assert authorize(check, "other", GITHUB).reason == "scope_not_connected"


def test_a_repo_check_is_allowed_only_under_a_connected_org() -> None:
    check = CHECKS["github.branch_protection"]

    assert authorize(check, "acme/app", GITHUB).allowed is True
    assert authorize(check, "other/app", GITHUB).reason == "scope_not_connected"
    assert authorize(check, "acme-evil/app", GITHUB).reason == "scope_not_connected"


def test_a_scope_of_the_wrong_shape_is_denied_before_anything_else() -> None:
    repo = CHECKS["github.branch_protection"]
    org = CHECKS["github.org_two_factor"]
    account = CHECKS["aws.root_mfa"]

    for scope in ["acme", "acme/app/extra", "acme/../x", "acme/app?x=1", "", "acme/ app"]:
        assert authorize(repo, scope, GITHUB).reason == "invalid_scope", scope
    for scope in ["acme/app", "ac me", "acme#"]:
        assert authorize(org, scope, GITHUB).reason == "invalid_scope", scope
    for scope in ["12345", "12345678901a", "1234567890123"]:
        assert authorize(account, scope, AWS).reason == "invalid_scope", scope


def test_a_connection_to_one_system_does_not_open_the_other() -> None:
    assert authorize(CHECKS["aws.root_mfa"], "123456789012", GITHUB).allowed is False
    assert authorize(CHECKS["aws.root_mfa"], "123456789012", AWS).allowed is True
    assert authorize(CHECKS["aws.root_mfa"], "999999999999", AWS).reason == "scope_not_connected"
