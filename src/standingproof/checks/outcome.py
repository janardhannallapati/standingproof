"""What one run of a check produced. Plain data: no HTTP, no database."""

from dataclasses import dataclass, field
from typing import Any, Literal

Status = Literal["observed", "unavailable", "not_configured", "denied"]


@dataclass(frozen=True)
class CheckOutcome:
    status: Status
    # One sentence a person can read. For "observed" it states the fact; otherwise it says
    # that nothing was observed, so it can never be mistaken for a finding.
    statement: str
    # A few named values, for code to compare. Empty unless the status is "observed".
    facts: dict[str, Any] = field(default_factory=dict)
    # The allow-listed fields of the system's reply. Empty unless "observed".
    raw: dict[str, Any] = field(default_factory=dict)
    # A short code for why nothing was observed: "forbidden", "not_found", "rate_limited"...
    reason: str | None = None


def observed(statement: str, facts: dict[str, Any], raw: dict[str, Any]) -> CheckOutcome:
    return CheckOutcome("observed", statement, facts, raw)


def unavailable(reason: str, detail: str) -> CheckOutcome:
    return CheckOutcome("unavailable", f"Not observed: {detail}", reason=reason)


def not_configured(detail: str) -> CheckOutcome:
    return CheckOutcome("not_configured", f"Not observed: {detail}", reason="not_configured")
