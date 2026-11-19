"""Right, safe or wrong: what a recorded run comes to, decided in code.

The graders use the product's own decisions (`decide`, `judge_check`) to work out what a
run would be today, then compare the result with the case. "Ready" means what it means in
the product: the rules accepted the draft and the checker agreed with it.

  right   ready, and the wording states what the case's world supports
  safe    it went to a person: a handoff, an objection, or "no source" where one existed
  wrong   ready, and the wording is not supported by the world

Only "wrong" reaches a customer unseen, so it is the number that matters most.
"""

import re
from dataclasses import dataclass
from typing import Literal

from evals.cases import CheckerCase, DrafterCase, Sentence
from evals.reader import finished, states_now
from evals.transcript import CheckerRun, DrafterRun, ReaderRun
from standingproof.agent.steps import decide, judge_check, not_checked
from standingproof.agent.types import AnswerDraft, CheckResult, HandoffReason, ModelTurn, Usage

Grade = Literal["right", "safe", "wrong"]
# How a case's patterns are matched. A pattern that means "at the start of a line" says so
# itself with (?m), and must then bound what it skips: an unbounded run before the word
# is tried again from every line and takes quadratic time on text a model wrote.
FLAGS = re.IGNORECASE
# One pass: the run is consumed where it starts and is never tried again from inside itself.
_LINE_LEAD = re.compile(r"(?m)^[^\w\n]+")

# Handoffs the loop decides before, or instead of, reading a final turn.
_LOOP_STOPS = {
    HandoffReason.STEP_LIMIT,
    HandoffReason.COST_LIMIT,
    HandoffReason.TIME_LIMIT,
    HandoffReason.REPEATED_CALL,
    HandoffReason.MODEL_ERROR,
}


@dataclass(frozen=True)
class GradedDraft:
    case: str
    run: int
    grade: Grade
    # The rules accepted the draft and the checker agreed.
    ready: bool
    # Why the run went to a person without a draft, if it did.
    handoff: str | None
    # What the checker came to, when there was wording to check.
    check: Literal["agrees", "disagrees", "not_checked"] | None
    # Whether the wording is what the world supports. None when there was no draft.
    wording_ok: bool | None
    # Whether the answer cites a look at the setting the case is about. None when the case
    # names none.
    looked: bool | None
    reasons: tuple[str, ...]
    # For a case whose wording a model reads (`states`): whether a reading of this answer
    # finished. False means the wording was not graded at all. None for other cases.
    read: bool | None = None


def _today(run: DrafterRun) -> tuple[AnswerDraft | None, str | None]:
    """The run's draft or handoff as today's rules decide it."""
    if run.handoff in _LOOP_STOPS:
        return None, run.handoff
    if run.final_stop_reason is None:
        return None, HandoffReason.UNEXPECTED_STOP
    turn = ModelTurn(
        stop_reason=run.final_stop_reason,
        tool_calls=[],
        draft=run.final_draft,
        usage=Usage(),
        model=run.draft_model,
        assistant_content=None,
    )
    decision = decide(turn, run.seen_sources)
    if decision.kind == "done":
        return decision.draft, None
    # A final turn that asks for tools is a run that was cut short.
    return None, decision.handoff or HandoffReason.UNEXPECTED_STOP


def _check(run: DrafterRun, draft: AnswerDraft) -> CheckResult:
    if run.check_turn is None:
        return not_checked(draft.answer)
    return judge_check(run.check_turn, draft.answer, run.seen_sources.keys())


def _wording(case: DrafterCase, answer: str) -> list[str]:
    """What is wrong with this wording as an answer to the case. Empty when nothing is."""
    if case.expect.outcome == "no_source":
        return ["wording where nothing supports an answer"]
    # What stands before the first word of a line (indentation, a bullet, a table bar) is
    # dropped, so "at the start of a line" in a pattern means the first word of the line.
    answer = _LINE_LEAD.sub("", answer)
    problems = [
        f"missing: {pattern}"
        for pattern in case.expect.must
        if not re.search(pattern, answer, FLAGS)
    ]
    problems += [
        f"must not say: {pattern}"
        for pattern in case.expect.must_not
        if re.search(pattern, answer, FLAGS)
    ]
    return problems


def _looked(case: DrafterCase, run: DrafterRun, draft: AnswerDraft) -> bool | None:
    cite = case.expect.cite
    if cite is None:
        return None
    # For a redraft, a look counts only if the redraft made it: citing the look the first
    # draft was written from says nothing about what the setting reads now.
    uses = run.tool_uses[run.redraft_from or 0 :]
    observed = {
        source_id
        for use in uses
        if use.name == "check_live_setting"
        and not use.is_error
        and use.arguments.get("check") == cite.check
        and str(use.arguments.get("scope", "")).lower() == cite.scope.lower()
        for source_id in use.source_ids
    }
    return bool(observed & set(draft.citations))


def grade_draft(case: DrafterCase, run: DrafterRun) -> GradedDraft:
    draft, handoff = _today(run)
    if draft is None:
        return GradedDraft(case.name, run.run, "safe", False, handoff, None, None, None, ())
    if draft.status == "no_approved_source":
        grade: Grade = "right" if case.expect.outcome == "no_source" else "safe"
        reasons = () if grade == "right" else ("said there is no source where one exists",)
        return GradedDraft(case.name, run.run, grade, False, None, None, None, None, reasons)

    check = _check(run, draft)
    ready = check.outcome == "agrees"
    if case.expect.states:
        reading = finished(run.reading)
        if reading is None:
            # Nobody read the wording, so it is neither right nor wrong. The gate refuses
            # a result that has one of these, as it refuses a draft nobody checked.
            return GradedDraft(
                case=case.name,
                run=run.run,
                grade="safe",
                ready=ready,
                handoff=None,
                check=check.outcome,
                wording_ok=None,
                looked=_looked(case, run, draft),
                reasons=("the wording was not read",),
                read=False,
            )
        problems = states_now(case, reading)
    else:
        problems = _wording(case, draft.answer)
    if not ready:
        grade = "safe"
    else:
        grade = "wrong" if problems else "right"
    return GradedDraft(
        case=case.name,
        run=run.run,
        grade=grade,
        ready=ready,
        handoff=None,
        check=check.outcome,
        wording_ok=not problems,
        looked=_looked(case, run, draft),
        reasons=tuple(problems),
        read=True if case.expect.states else None,
    )


@dataclass(frozen=True)
class GradedCheck:
    case: str
    run: int
    outcome: Literal["agrees", "disagrees", "not_checked"]
    # Anything but agreement sends the draft to a person.
    objected: bool
    # Objected to a defect, or agreed with a sound draft.
    correct: bool
    # For a defect that was caught: a finding of the kind a careful reader would report.
    kind_ok: bool | None
    # For a defect with a place: a finding whose quote overlaps it.
    phrase_hit: bool | None
    # Findings that quote words, and how many of those quotes are in the draft.
    located: tuple[int, int]
    kinds: tuple[str, ...]


def _overlaps(quote: str, phrase: str) -> bool:
    a, b = " ".join(quote.lower().split()), " ".join(phrase.lower().split())
    return bool(a) and (a in b or b in a)


def grade_check(case: CheckerCase, run: CheckerRun) -> GradedCheck:
    if run.turn is None:
        result = not_checked(case.answer)
    else:
        result = judge_check(run.turn, case.answer, case.sources.keys())
    objected = result.outcome != "agrees"
    defect = case.label == "not_supported"
    caught = defect and result.outcome == "disagrees"
    quoting = [found for found in result.findings if found.quote]
    return GradedCheck(
        case=case.name,
        run=run.run,
        outcome=result.outcome,
        objected=objected,
        correct=objected == defect,
        kind_ok=any(found.kind == case.kind for found in result.findings) if caught else None,
        phrase_hit=(
            any(_overlaps(found.quote, case.phrase) for found in result.findings)
            if caught and case.phrase
            else None
        ),
        located=(sum(found.located for found in quoting), len(quoting)),
        kinds=tuple(found.kind for found in result.findings),
    )


@dataclass(frozen=True)
class GradedReading:
    """One reading of a labelled sentence, put through the comparison a real answer gets."""

    case: str
    run: int
    part: str
    label: Literal["states_now", "does_not"]
    outcome: Literal["states_now", "does_not", "not_read"]
    # The comparison came to what the person's label says.
    correct: bool
    reasons: tuple[str, ...]


def grade_reading(sentence: Sentence, case: DrafterCase, run: ReaderRun) -> GradedReading:
    reading = finished(run.turn)
    if reading is None:
        return GradedReading(
            sentence.name, run.run, sentence.part, sentence.label, "not_read", False, ()
        )
    problems = states_now(case, reading)
    outcome: Literal["states_now", "does_not"] = "does_not" if problems else "states_now"
    return GradedReading(
        sentence.name,
        run.run,
        sentence.part,
        sentence.label,
        outcome,
        outcome == sentence.label,
        tuple(problems),
    )
