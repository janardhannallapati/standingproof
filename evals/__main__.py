"""The eval's commands.

uv run python -m evals estimate [--smoke]     what a run would cost; calls nothing
uv run python -m evals run [--smoke]          the paid run; asks first, stops at a ceiling
uv run python -m evals run --family G --runs 1    one kind of drafter case only
uv run python -m evals score                  grade the recordings against the baseline (CI)
uv run python -m evals accept --note "..."    make the recorded result the baseline
uv run python -m evals report                 write evals/REPORT.md from the recordings
"""

import argparse
import asyncio
import datetime as dt
import json
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from evals.cases import SETS, load_checker, load_drafter
from evals.fingerprint import Config, settings
from evals.plan import Plan, approved, estimate, plan_for
from evals.report import render
from evals.result import load_result, not_whole, problems
from evals.score import BASELINE, as_baseline, load_baseline
from evals.transcript import RECORDINGS, MisplacedError

if TYPE_CHECKING:
    import anthropic

REPORT = Path(__file__).parent / "REPORT.md"
CEILING, SMOKE_CEILING = 4.00, 0.75
# No single run may be given more than this, whoever asks.
HIGHEST_CEILING = 10.00
PROVIDER = "https://api.anthropic.com"
NOT_APPROVED, INCOMPLETE = 2, 3


def provider_client(key: str) -> "anthropic.AsyncAnthropic":
    """The real client. Its address is fixed here: the SDK would otherwise take one from
    ANTHROPIC_BASE_URL and send the key there. It does not retry: a retry inside the client
    is a request the run's meter never sees, and the run retries a case once itself."""
    import anthropic

    return anthropic.AsyncAnthropic(api_key=key, base_url=PROVIDER, timeout=120.0, max_retries=0)


def real_run(plan: Plan, *, ceiling: float, root: Path, out: Callable[[str], None]) -> int:
    """Start a throwaway Postgres, the real embedding model and the real clients, and run."""
    from alembic.config import Config as AlembicConfig
    from testcontainers.postgres import PostgresContainer

    from alembic import command
    from evals.run import Clients, run_plan
    from standingproof.agent.model import AnthropicChecker, AnthropicModel
    from standingproof.config import Settings, get_settings
    from standingproof.db import build_engine, build_session_factory
    from standingproof.library.embedding import build_embedder

    key = Settings().anthropic_api_key
    if key is None:
        out("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).")
        return NOT_APPROVED
    config = Config.current()

    async def go(url: str) -> bool:
        client = provider_client(key.get_secret_value())
        engine = build_engine(url)
        try:
            manifest = await run_plan(
                plan,
                sessions=build_session_factory(engine),
                embedder=build_embedder(settings()),
                clients=Clients(
                    model=AnthropicModel(
                        client,
                        model=config.draft_model,
                        effort=config.draft_effort,  # type: ignore[arg-type]
                    ),
                    checker=AnthropicChecker(
                        client,
                        model=config.check_model,
                        effort=config.check_effort,  # type: ignore[arg-type]
                    ),
                    cheaper_checker=AnthropicChecker(
                        client,
                        model=config.cheaper_check_model,
                        effort=config.check_effort,  # type: ignore[arg-type]
                    ),
                ),
                config=config,
                ceiling=ceiling,
                root=root,
                say=out,
            )
        finally:
            await engine.dispose()
            await client.close()
        out(
            f"recorded {manifest.recorded}, reused {manifest.reused}, "
            f"spent ${manifest.spent_usd:.4f} of a ceiling of ${ceiling:.2f}"
        )
        if not manifest.complete:
            out(f"INCOMPLETE: {manifest.stopped}")
        return manifest.complete

    with PostgresContainer("pgvector/pgvector:pg17", driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        os.environ["SP_DATABASE_URL"] = url
        get_settings.cache_clear()
        command.upgrade(AlembicConfig("alembic.ini"), "head")
        return 0 if asyncio.run(go(url)) else INCOMPLETE


def _print_estimate(plan: Plan, ceiling: float, out: Callable[[str], None]) -> None:
    cost = estimate(plan)
    for line in cost.lines:
        out(f"{line.calls:>5} x ${line.each:.4f}  {line.what:<40} ${line.total:.2f}")
    out(f"estimate ${cost.total:.2f}; ceiling ${ceiling:.2f}, checked before every request")


def main(
    argv: list[str] | None = None,
    *,
    sets: Path = SETS,
    root: Path = RECORDINGS,
    baseline: Path = BASELINE,
    report: Path = REPORT,
    out: Callable[[str], None] = print,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("estimate", "run"):
        sub = commands.add_parser(name)
        sub.add_argument("--smoke", action="store_true", help="one case of each kind")
        sub.add_argument("--ceiling", type=float, help="stop before spending more than this")
        sub.add_argument("--approved-usd", type=float, help="the ceiling a person agreed to")
        sub.add_argument("--family", choices=list("ABCDEFG"), help="only this kind of case")
        sub.add_argument("--runs", type=int, choices=[1, 2, 3], help="with --family: runs each")
    commands.add_parser("score")
    commands.add_parser("report")
    commands.add_parser("accept").add_argument("--note", required=True)
    args = parser.parse_args(argv)

    drafter, checker = load_drafter(sets / "drafter.json"), load_checker(sets / "checker.json")
    if args.command in ("estimate", "run"):
        if args.runs is not None and args.family is None:
            out("--runs goes with --family: a full run is always three of each case")
            return NOT_APPROVED
        plan = plan_for(drafter, checker, smoke=args.smoke, family=args.family, runs=args.runs or 3)
        ceiling = SMOKE_CEILING if args.smoke else CEILING
        if args.ceiling is not None:
            ceiling = args.ceiling
        if not (math.isfinite(ceiling) and 0 < ceiling <= HIGHEST_CEILING):
            out(f"the ceiling must be more than $0 and at most ${HIGHEST_CEILING:.2f}")
            return NOT_APPROVED
        _print_estimate(plan, ceiling, out)
        if args.command == "estimate":
            return 0
        agreed = approved(
            ceiling=ceiling, stated=args.approved_usd, terminal=sys.stdin.isatty(), ask=input
        )
        if not agreed:
            out("not approved: nothing was sent. Type yes at a terminal, or pass --approved-usd")
            return NOT_APPROVED
        return real_run(plan, ceiling=ceiling, root=root, out=out)

    try:
        result = load_result(root, drafter, checker, sets)
    except MisplacedError as error:
        out(f"FAIL: {error}")
        return 1
    if args.command == "report":
        report.write_text(render(result, load_baseline(baseline)))
        out(f"wrote {report}")
        return 0
    d, c = result.drafter, result.checker
    out(
        f"drafter: {d.right_cases} of {d.cases} cases right, "
        f"{len(d.wrong_cases)} ready and wrong; checker: {len(c.missed_cases)} defects "
        f"agreed with, {len(c.false_alarm_cases)} sound drafts objected to"
    )
    if args.command == "accept":
        found = not_whole(result)
        for problem in found:
            out(f"REFUSED: {problem}")
        if found:
            return 1
        today = dt.datetime.now(dt.UTC).date().isoformat()
        accepted = as_baseline(d, c, on=today, note=args.note)
        baseline.write_text(json.dumps(accepted.model_dump(), indent=1) + "\n")
        out(f"wrote {baseline}")
        return 0
    found = problems(result, load_baseline(baseline))
    for problem in found:
        out(f"FAIL: {problem}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
