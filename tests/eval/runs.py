"""Hand-written recordings for the eval's own tests: what a run of a case could have left."""

from typing import Any

from evals.transcript import CheckerRun, DrafterRun, ToolUse
from standingproof.agent.types import AnswerDraft, CheckTurn, CheckVerdict, Finding, Usage

EVIDENCE = "evidence:11111111-1111-1111-1111-111111111111"
LIBRARY = "answer:22222222-2222-2222-2222-222222222222"


def verdict(*findings: Finding, stop_reason: str = "end_turn") -> CheckTurn:
    return CheckTurn(
        stop_reason=stop_reason,
        verdict=CheckVerdict(
            verdict="not_supported" if findings else "supported", findings=list(findings)
        ),
        usage=Usage(input_tokens=900, output_tokens=60),
        model="claude-sonnet-5-5",
    )


def finding(kind: str = "unsupported", quote: str = "", source_id: str = "") -> Finding:
    return Finding(kind=kind, quote=quote, source_id=source_id, explanation="Because.")  # type: ignore[arg-type]


AGREES = verdict()
OBJECTS = verdict(finding())


def looked(check: str, scope: str, source_id: str = EVIDENCE) -> ToolUse:
    return ToolUse(
        name="check_live_setting",
        arguments={"check": check, "scope": scope},
        is_error=False,
        source_ids=[source_id],
    )


def drafted(
    case: str,
    answer: str = "",
    citations: list[str] | None = None,
    *,
    sources: dict[str, str] | None = None,
    check: CheckTurn | None = AGREES,
    tool_uses: list[ToolUse] | None = None,
    handoff: str | None = None,
    run: int = 1,
    **more: Any,
) -> DrafterRun:
    """A run that ended with a draft. No citations and no wording means "no approved source"."""
    cited = citations or []
    draft = AnswerDraft(
        status="answered" if answer else "no_approved_source",
        answer=answer,
        citations=cited,
        note="",
    )
    seen = {source_id: [text] for source_id, text in (sources or {}).items()}
    for source_id in cited:
        seen.setdefault(source_id, ["Source text."])
    values: dict[str, Any] = {
        "case": case,
        "run": run,
        "variant": "full",
        "fingerprint": {},
        "handoff": handoff,
        "final_stop_reason": "end_turn",
        "final_draft": draft,
        "steps": 2,
        "tool_uses": tool_uses or [],
        "seen_sources": seen,
        "check_turn": check if answer else None,
        "usage": Usage(input_tokens=2000, output_tokens=300),
        "cost_usd": 0.02,
        "draft_model": "claude-opus-5-5",
        "check_model": "claude-sonnet-5-5",
    }
    return DrafterRun(**(values | more))


def stopped(case: str, handoff: str, run: int = 1) -> DrafterRun:
    """A run the loop ended itself: a limit, a repeated call, a provider failure."""
    return drafted(case, run=run).model_copy(
        update={"handoff": handoff, "final_draft": None, "final_stop_reason": "tool_use"}
    )


def checked(case: str, turn: CheckTurn | None = AGREES, run: int = 1, **more: Any) -> CheckerRun:
    values: dict[str, Any] = {
        "case": case,
        "run": run,
        "variant": "main",
        "fingerprint": {},
        "turn": turn,
        "cost_usd": 0.002,
        "check_model": "claude-sonnet-5-5",
    }
    return CheckerRun(**(values | more))
