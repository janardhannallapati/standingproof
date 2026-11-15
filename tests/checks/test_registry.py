from standingproof.checks.registry import CHECKS, READ_ONLY_OPERATIONS


def test_the_seven_checks_are_registered_under_their_names() -> None:
    assert set(CHECKS) == {
        "github.org_two_factor",
        "github.branch_protection",
        "github.secret_scanning",
        "aws.root_mfa",
        "aws.password_policy",
        "aws.s3_public_access_block",
        "aws.ebs_default_encryption",
    }
    for name, check in CHECKS.items():
        assert check.name == name
        assert name.startswith(f"{check.system}.")
        assert check.description.endswith(".")


def test_every_operation_a_check_declares_is_on_the_read_only_list() -> None:
    for check in CHECKS.values():
        assert check.operations, check.name
        assert set(check.operations) <= READ_ONLY_OPERATIONS, check.name


def test_the_read_only_list_holds_nothing_that_could_change_anything() -> None:
    for operation in READ_ONLY_OPERATIONS:
        if operation.startswith("GET "):
            continue
        service, _, action = operation.partition(":")
        assert service in {"sts", "iam", "s3control", "ec2"}, operation
        assert action.startswith("Get"), operation


def test_scope_kinds_match_the_system() -> None:
    assert {c.scope_kind for c in CHECKS.values() if c.system == "aws"} == {"account"}
    assert {c.scope_kind for c in CHECKS.values() if c.system == "github"} == {"org", "repo"}
