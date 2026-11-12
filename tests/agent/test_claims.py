from standingproof.agent.claims import unsupported_claims


def test_flags_a_strong_claim_the_sources_do_not_make() -> None:
    assert unsupported_claims("We are SOC 2 compliant.", ["MFA is required."]) == ["compliant"]


def test_allows_a_strong_claim_when_a_cited_source_makes_it() -> None:
    assert unsupported_claims("We are ISO 27001 certified.", ["We are ISO 27001 certified."]) == []


def test_matching_ignores_case() -> None:
    assert unsupported_claims("Uptime is GUARANTEED.", ["no promise here"]) == ["guarantee"]


def test_plain_wording_passes() -> None:
    assert unsupported_claims("Yes. MFA is required.", ["MFA is required."]) == []
