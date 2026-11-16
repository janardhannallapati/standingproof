"""What a recorded reply depends on, hashed part by part.

A recording answers "what did the model say to this request?". It stops being an answer
when the request changes: another prompt, tool description, output schema, model, effort,
or case. Each of those is a named part here. CI compares the parts stored in every
recording with today's; a difference means the eval must be run again, and the name of the
part says why.

Not covered: what the tools return. A change to search ranking or to a check's sentence
changes what the drafter reads without changing any part below.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from evals.cases import CheckerCase, DrafterCase, DrafterSet
from standingproof.agent import prompts
from standingproof.agent.checker import check_messages
from standingproof.agent.tools import ToolRegistry
from standingproof.agent.types import AnswerDraft, CheckVerdict
from standingproof.checks.service import CheckBackends
from standingproof.checks.tools import check_tool
from standingproof.config import Settings
from standingproof.library.embedding import Embedder
from standingproof.library.tools import build_library_registry

DrafterVariant = Literal["full", "docs_only"]
CheckerVariant = Literal["main", "cheaper"]


def settings(**values: Any) -> Settings:
    """The product's settings with no .env file read: an eval takes nothing from a deployment."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _default(field: str) -> Any:
    # The product's default, whatever this machine's environment says.
    return Settings.model_fields[field].default


@dataclass(frozen=True)
class Config:
    """The models and limits an eval runs with. Recorded, so never read from the environment."""

    draft_model: str
    draft_effort: str
    check_model: str
    check_effort: str
    max_steps: int
    max_cost_usd: float
    max_seconds: float
    cheaper_check_model: str = "claude-haiku-5-5"

    @classmethod
    def current(cls) -> "Config":
        return cls(
            draft_model=_default("draft_model"),
            draft_effort=_default("draft_effort"),
            check_model=_default("check_model"),
            check_effort=_default("check_effort"),
            max_steps=_default("run_max_steps"),
            max_cost_usd=_default("run_max_cost_usd"),
            max_seconds=_default("run_max_seconds"),
        )


def _sha(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def tool_definitions(variant: DrafterVariant) -> list[dict[str, Any]]:
    """The tool list the drafter is sent. Building it touches no database and no system."""
    nothing = cast(async_sessionmaker[AsyncSession], None)
    tenant = uuid.UUID(int=0)
    registry: ToolRegistry = build_library_registry(nothing, tenant, cast(Embedder, None))
    if variant == "full":
        backends = CheckBackends(github=None, aws=None)
        registry = registry.with_tools(check_tool(nothing, tenant, backends, settings()))
    return registry.definitions()


def _checker_parts(config: Config, variant: CheckerVariant) -> dict[str, str]:
    sample = AnswerDraft(status="answered", answer="A.", citations=["S-1"], note="")
    return {
        "checker.prompt": _sha(prompts.CHECKER_PROMPT),
        "checker.schema": _sha(CheckVerdict.model_json_schema()),
        "checker.request": _sha(check_messages("Q?", sample, {"S-1": ["T."], "S-2": ["U."]})),
        "checker.model": config.check_model if variant == "main" else config.cheaper_check_model,
        "checker.effort": config.check_effort,
    }


def of_drafter_case(
    cases: DrafterSet, case: DrafterCase, variant: DrafterVariant, config: Config
) -> dict[str, str]:
    world = {
        "case": case.model_dump(mode="json", exclude={"note"}),
        "library": cases.library(case.library).model_dump(mode="json"),
    }
    return {
        "drafter.prompt": _sha(prompts.SYSTEM_PROMPT),
        "drafter.tools": _sha(tool_definitions(variant)),
        "drafter.schema": _sha(AnswerDraft.model_json_schema()),
        "drafter.request": _sha(prompts.question_message("Q?")),
        "drafter.model": config.draft_model,
        "drafter.effort": config.draft_effort,
        "drafter.limits": f"{config.max_steps}/{config.max_cost_usd}/{config.max_seconds}",
        **_checker_parts(config, "main"),
        "case": _sha(world),
    }


def of_checker_case(case: CheckerCase, variant: CheckerVariant, config: Config) -> dict[str, str]:
    return {**_checker_parts(config, variant), "case": _sha(case.model_dump(mode="json"))}


def differences(recorded: dict[str, str], today: dict[str, str]) -> list[str]:
    """The parts that are not the same now as when the recording was made."""
    return sorted(
        part for part in recorded.keys() | today.keys() if recorded.get(part) != today.get(part)
    )
