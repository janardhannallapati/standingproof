"""Run the cases against a model, under a ceiling, and leave recordings behind.

This is the only part of the eval that can spend money. It is given its clients, so the
tests drive it with scripted ones; `__main__` gives it real ones after a person has said yes.

Rules it keeps:
- No request is sent unless the ceiling has room for the most that one request could cost.
  Every completed request is counted, whether or not its case ended in a recording.
- A provider failure is not a result. The case is tried once more, then the run stops and
  is marked incomplete; an incomplete run cannot become the baseline.
- A case already recorded from the same inputs is not paid for again.
- The manifest says "did not finish" from the first moment, so a run that dies cannot be
  mistaken for the run before it.

What the ceiling cannot see: a request the provider billed but never answered (a timeout),
and a second run started beside this one. Each run has its own ceiling.
"""

import datetime as dt
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.cases import CheckerCase, CheckerSet, DrafterCase, DrafterSet, load_checker, load_drafter
from evals.fingerprint import (
    CheckerVariant,
    Config,
    DrafterVariant,
    of_checker_case,
    of_drafter_case,
)
from evals.plan import Manifest, Plan
from evals.transcript import MANIFEST, CheckerRun, DrafterRun, Run, ToolUse, path_of, write
from evals.world import build_world
from standingproof.agent.checker import CheckerClient, run_check
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelCallError, ModelClient, run_agent
from standingproof.agent.pricing import cost_usd
from standingproof.agent.tools import ToolOutcome, ToolRegistry
from standingproof.agent.types import (
    AnswerDraft,
    CheckTurn,
    HandoffReason,
    ModelTurn,
    ToolCall,
    Usage,
)
from standingproof.library.embedding import Embedder

# The most one request could cost, in US dollars. Drafter: its output limit of 16,000 tokens
# at $20 a million is $0.32, and 20,000 tokens of input, far more than any case here sends,
# is $0.08. Checker: 8,000 tokens out at $10 a million, and the same allowance for input.
DRAFT_CALL_AT_MOST = 0.40
CHECK_AT_MOST = 0.10


class _CeilingReachedError(Exception):
    pass


class Meter:
    """What the run has spent, and whether one more request fits under the ceiling."""

    def __init__(self, ceiling: float) -> None:
        self.ceiling = ceiling
        self.spent = 0.0

    def room(self, at_most: float) -> bool:
        return self.spent + at_most <= self.ceiling

    def before(self, at_most: float) -> None:
        if not self.room(at_most):
            raise _CeilingReachedError

    def add(self, model: str, usage: Usage) -> None:
        self.spent += cost_usd(model, usage)


@dataclass
class Clients:
    model: ModelClient
    checker: CheckerClient
    cheaper_checker: CheckerClient


class _LastTurn:
    """Passes turns through and remembers the last one, as the model gave it."""

    def __init__(self, inner: ModelClient, meter: Meter) -> None:
        self._inner = inner
        self._meter = meter
        self.last: ModelTurn | None = None

    async def complete(self, **request: Any) -> ModelTurn:
        self._meter.before(DRAFT_CALL_AT_MOST)
        self.last = await self._inner.complete(**request)
        self._meter.add(self.last.model, self.last.usage)
        return self.last


class _LastCheck:
    def __init__(self, inner: CheckerClient, meter: Meter) -> None:
        self._inner = inner
        self._meter = meter
        # Whether the checker was asked at all. The loop skips it when a case is at its limit.
        self.asked = False
        self.turn: CheckTurn | None = None

    async def check(self, *, system: str, messages: list[dict[str, Any]]) -> CheckTurn:
        self._meter.before(CHECK_AT_MOST)
        self.asked = True
        self.turn = await self._inner.check(system=system, messages=messages)
        self._meter.add(self.turn.model, self.turn.usage)
        return self.turn


class _Recorded:
    """The registry's two methods, with every use written down."""

    def __init__(self, inner: ToolRegistry) -> None:
        self._inner = inner
        self.uses: list[ToolUse] = []
        self.sources: dict[str, list[str]] = {}

    def definitions(self) -> list[dict[str, Any]]:
        return self._inner.definitions()

    async def execute(self, call: ToolCall) -> ToolOutcome:
        outcome = await self._inner.execute(call)
        self.uses.append(
            ToolUse(
                name=call.name,
                arguments=call.arguments,
                is_error=outcome.is_error,
                source_ids=list(outcome.sources),
            )
        )
        for source_id, text in outcome.sources.items():
            self.sources.setdefault(source_id, []).append(text)
        return outcome


class _ProviderFailedError(Exception):
    """The provider could not be reached. What the attempt cost is already on the meter."""


async def _draft(
    sessions: async_sessionmaker[AsyncSession],
    cases: DrafterSet,
    case: DrafterCase,
    number: int,
    variant: DrafterVariant,
    clients: Clients,
    embedder: Embedder,
    config: Config,
    meter: Meter,
) -> DrafterRun:
    world = await build_world(sessions, cases, case, embedder=embedder, variant=variant)
    model = _LastTurn(clients.model, meter)
    checker = _LastCheck(clients.checker, meter)
    registry = _Recorded(world.registry)
    try:
        result = await run_agent(
            question=case.question,
            model=model,
            registry=cast(ToolRegistry, registry),
            limits=Limits(config.max_steps, config.max_cost_usd, config.max_seconds),
            checker=checker,
        )
    finally:
        await world.aclose()
    # A check that was asked for and did not come back. One the loop skipped because the
    # case had reached its own limit is a result, and is recorded as not checked.
    check_failed = checker.asked and checker.turn is None
    if result.handoff == HandoffReason.MODEL_ERROR or check_failed:
        raise _ProviderFailedError
    return DrafterRun(
        case=case.name,
        run=number,
        variant=variant,
        fingerprint=of_drafter_case(cases, case, variant, config),
        handoff=result.handoff,
        final_stop_reason=model.last.stop_reason if model.last else None,
        final_draft=model.last.draft if model.last else None,
        steps=result.steps,
        tool_uses=registry.uses,
        seen_sources=registry.sources,
        check_turn=checker.turn,
        usage=result.usage,
        cost_usd=result.cost_usd,
        draft_model=config.draft_model,
        check_model=config.check_model,
    )


async def _check(
    case: CheckerCase,
    number: int,
    variant: CheckerVariant,
    checker: CheckerClient,
    config: Config,
    meter: Meter,
) -> CheckerRun:
    seen = _LastCheck(checker, meter)
    draft = AnswerDraft(status="answered", answer=case.answer, citations=case.citations, note="")
    try:
        result, _ = await run_check(seen, question=case.question, draft=draft, sources=case.sources)
    except ModelCallError:
        raise _ProviderFailedError from None
    return CheckerRun(
        case=case.name,
        run=number,
        variant=variant,
        fingerprint=of_checker_case(case, variant, config),
        turn=seen.turn,
        cost_usd=result.cost_usd,
        check_model=config.check_model if variant == "main" else config.cheaper_check_model,
    )


@dataclass(frozen=True)
class _Unit:
    """One case to run once: where its recording goes, the most it could cost, and how."""

    kind: str
    variant: str
    name: str
    number: int
    at_most: float
    fingerprint: dict[str, str]
    start: Callable[[], Awaitable[Run]]

    def recorded(self, root: Path) -> bool:
        """Whether this case already has a recording made from today's inputs."""
        path = root / f"{self.kind}-{self.variant}" / f"{self.name}.{self.number}.json"
        if not path.exists():
            return False
        return bool(json.loads(path.read_text()).get("fingerprint") == self.fingerprint)


def _units(
    plan: Plan,
    drafts: DrafterSet,
    checks: CheckerSet,
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    clients: Clients,
    config: Config,
    meter: Meter,
) -> list[_Unit]:
    units: list[_Unit] = []
    drafter_plan: list[tuple[DrafterVariant, list[tuple[str, int]]]] = [
        ("full", plan.drafter),
        ("docs_only", [(name, 1) for name in plan.docs_only]),
    ]
    for drafter_variant, pairs in drafter_plan:
        for name, number in pairs:
            case = drafts.by_name[name]
            start = partial(
                _draft,
                sessions,
                drafts,
                case,
                number,
                drafter_variant,
                clients,
                embedder,
                config,
                meter,
            )
            today = of_drafter_case(drafts, case, drafter_variant, config)
            units.append(
                _Unit("drafter", drafter_variant, name, number, DRAFT_CALL_AT_MOST, today, start)
            )
    checker_plan: list[tuple[CheckerVariant, list[tuple[str, int]], CheckerClient]] = [
        ("main", plan.checker, clients.checker),
        ("cheaper", [(name, 1) for name in plan.cheaper], clients.cheaper_checker),
    ]
    for checker_variant, pairs, client in checker_plan:
        for name, number in pairs:
            labelled = checks.by_name[name]
            units.append(
                _Unit(
                    "checker",
                    checker_variant,
                    name,
                    number,
                    CHECK_AT_MOST,
                    of_checker_case(labelled, checker_variant, config),
                    partial(_check, labelled, number, checker_variant, client, config, meter),
                )
            )
    return units


def _save_manifest(manifest: Manifest, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MANIFEST).write_text(json.dumps(manifest.model_dump(), indent=1) + "\n")


async def run_plan(
    plan: Plan,
    *,
    sessions: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    clients: Clients,
    config: Config,
    ceiling: float,
    root: Path,
    say: Callable[[str], None] = print,
    drafter: DrafterSet | None = None,
    checker: CheckerSet | None = None,
) -> Manifest:
    drafts = drafter or load_drafter()
    checks = checker or load_checker()
    meter = Meter(ceiling)
    recorded, reused = 0, 0
    full = f"the ceiling of ${ceiling:.2f} has no room for another request"

    def manifest(stopped: str) -> Manifest:
        return Manifest(
            complete=not stopped,
            stopped=stopped,
            spent_usd=meter.spent,
            recorded=recorded,
            reused=reused,
            finished_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        )

    stopped = "the run did not finish"
    _save_manifest(manifest(stopped), root)
    try:
        stopped = ""
        for unit in _units(plan, drafts, checks, sessions, embedder, clients, config, meter):
            if unit.recorded(root):
                reused += 1
                continue
            run: Run | None = None
            for _attempt in range(2):
                if not meter.room(unit.at_most):
                    stopped = full
                    break
                try:
                    run = await unit.start()
                    break
                except _CeilingReachedError:
                    stopped = full
                    break
                except _ProviderFailedError:
                    continue
            else:
                stopped = "the model provider could not be reached; this case was not recorded"
            if run is None:
                break
            recorded += 1
            write(run, root)
            say(
                f"{path_of(run, root).relative_to(root)}  ${run.cost_usd:.4f}  "
                f"total ${meter.spent:.4f}"
            )
    except BaseException:
        stopped = "the run did not finish"
        raise
    finally:
        _save_manifest(manifest(stopped), root)
    return manifest(stopped)
