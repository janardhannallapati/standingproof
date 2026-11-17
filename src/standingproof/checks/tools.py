"""The agent's read-only tool for looking at a live setting.

The model chooses a check by name from the registry and names a scope. Whether that is
allowed is decided by the same code as for any other caller; the model cannot widen it.
"""

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from standingproof.agent.tools import OBSERVED_PREFIX, Tool, ToolOutcome
from standingproof.checks.registry import CHECKS
from standingproof.checks.service import CheckBackends, run_check, source_text
from standingproof.config import Settings

# The schema the model is given lists the checks, so it cannot ask for one that is not there.
_CHECK_NAMES: dict[str, Any] = {"enum": sorted(CHECKS)}


class CheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check: str = Field(
        description="The name of the check to run.",
        json_schema_extra=_CHECK_NAMES,
    )
    scope: str = Field(
        description=(
            "What to check: a GitHub organisation ('acme'), a repository ('acme/app'), "
            "or a 12-digit AWS account id."
        )
    )


_NO_SECOND_TRY = (
    "Do not make this same call again for this question. Nothing was observed, so do not "
    "state this setting either way."
)


def _description() -> str:
    lines = [
        f"- {check.name} ({check.scope_kind}): {check.description}" for check in CHECKS.values()
    ]
    return (
        "Look at a setting in the company's own GitHub or AWS account, right now, read-only. "
        "Use it when a question asks whether something is enforced and an approved answer "
        "alone would be a claim. The result states what was seen and when, and can be cited. "
        "If it says nothing was observed, do not state the setting either way.\n"
        "Checks:\n" + "\n".join(lines)
    )


def check_tool(
    sessions: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    backends: CheckBackends,
    settings: Settings,
) -> Tool:
    """The tool for one run, bound to one tenant."""

    async def check(arguments: CheckInput) -> ToolOutcome:
        async with sessions() as session:
            evidence = await run_check(
                session,
                tenant_id=tenant_id,
                name=arguments.check,
                scope=arguments.scope,
                backends=backends,
                settings=settings,
            )
        if evidence.status == "denied":
            return ToolOutcome(
                content=f"Error: this check is not available here ({evidence.reason}). "
                "Use a check and a scope the company has connected.",
                is_error=True,
            )
        if evidence.status != "observed":
            # No source: an answer cannot cite something that was not seen. The loop ends a
            # run that makes the same call twice, so the result says not to try again.
            return ToolOutcome(
                content=f"{evidence.statement} Reason: {evidence.reason}. {_NO_SECOND_TRY}"
            )
        source_id = f"{OBSERVED_PREFIX}{evidence.id}"
        return ToolOutcome(
            content=json.dumps(
                {
                    "id": source_id,
                    "check": evidence.check_name,
                    "scope": evidence.scope,
                    "statement": evidence.statement,
                    "facts": evidence.facts,
                    "observed_at": evidence.observed_at.isoformat(),
                }
            ),
            sources={source_id: source_text(evidence)},
        )

    return Tool(
        name="check_live_setting",
        description=_description(),
        input_model=CheckInput,
        handler=check,
        read_only=True,
    )
