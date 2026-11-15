from standingproof.checks.redact import keep


def test_only_listed_fields_survive() -> None:
    reply = {"login": "acme", "two_factor_requirement_enabled": True, "billing_email": "a@b.c"}

    assert keep(reply, ["two_factor_requirement_enabled"]) == {
        "two_factor_requirement_enabled": True
    }


def test_nested_fields_are_addressed_with_dots() -> None:
    reply = {
        "required_pull_request_reviews": {"required_approving_review_count": 1, "url": "https://x"},
        "enforce_admins": {"enabled": False, "url": "https://y"},
    }

    assert keep(
        reply,
        ["required_pull_request_reviews.required_approving_review_count", "enforce_admins.enabled"],
    ) == {
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "enforce_admins": {"enabled": False},
    }


def test_a_listed_field_that_is_absent_is_simply_absent() -> None:
    assert keep({"a": 1}, ["b", "c.d"]) == {}
    assert keep({"c": "not a dict"}, ["c.d"]) == {}


def test_a_secret_or_an_account_number_in_the_reply_does_not_survive() -> None:
    reply = {
        "Account": "123456789012",
        "Arn": "arn:aws:iam::123456789012:user/reader",
        "token": "github_pat_11AAAAAAA0secretsecretsecret",
        "SummaryMap": {"AccountMFAEnabled": 1, "Users": 4},
    }

    kept = keep(reply, ["SummaryMap.AccountMFAEnabled"])

    assert kept == {"SummaryMap": {"AccountMFAEnabled": 1}}
    assert "123456789012" not in str(kept)
    assert "github_pat" not in str(kept)


def test_only_plain_values_are_kept_never_a_whole_subtree() -> None:
    # Listing "owner" must not copy everything under it.
    assert keep({"owner": {"login": "acme", "email": "x@y.z"}}, ["owner"]) == {}
