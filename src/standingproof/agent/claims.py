"""Wording the model may use only when a cited source uses it too."""

# Each of these turns a description into a promise or a legal status. A questionnaire
# answer is often incorporated into a contract, so they need a source, not a model's say-so.
STRONG_CLAIM_TERMS: tuple[str, ...] = ("compliant", "certified", "guarantee", "accredited")


def unsupported_claims(answer: str, cited_texts: list[str]) -> list[str]:
    """Return the strong terms that appear in `answer` but in none of the cited sources."""
    answer_lower = answer.lower()
    sources_lower = " ".join(cited_texts).lower()
    return [
        term for term in STRONG_CLAIM_TERMS if term in answer_lower and term not in sources_lower
    ]
