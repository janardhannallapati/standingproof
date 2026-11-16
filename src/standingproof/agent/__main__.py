"""Run the agent on one question:  uv run python -m standingproof.agent "your question"."""

import argparse
import asyncio
import sys
from pathlib import Path

import anthropic

from standingproof.agent.library import LIBRARY, build_registry
from standingproof.agent.limits import Limits
from standingproof.agent.loop import ModelClient, run_agent
from standingproof.agent.model import AnthropicChecker, AnthropicModel
from standingproof.agent.recording import RecordingModel
from standingproof.config import get_settings


async def _main(question: str, record_to: Path | None) -> int:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).", file=sys.stderr)
        return 2

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=60.0, max_retries=2
    )
    model: ModelClient = AnthropicModel(
        client, model=settings.draft_model, effort=settings.draft_effort
    )
    if record_to is not None:
        model = RecordingModel(model, record_to)

    result = await run_agent(
        question=question,
        model=model,
        registry=build_registry(LIBRARY),
        limits=Limits(
            max_steps=settings.run_max_steps,
            max_cost_usd=settings.run_max_cost_usd,
            max_seconds=settings.run_max_seconds,
        ),
        checker=(
            AnthropicChecker(client, model=settings.check_model, effort=settings.check_effort)
            if settings.check_enabled
            else None
        ),
    )

    if result.draft is not None:
        print(f"status:    {result.draft.status}")
        print(f"answer:    {result.draft.answer}")
        print(f"citations: {', '.join(result.draft.citations) or '-'}")
        print(f"note:      {result.draft.note or '-'}")
    else:
        print(f"handed to a person: {result.handoff}")
    if result.check is not None:
        print(f"checker:   {result.check.outcome}")
        for found in result.check.findings:
            print(f"  {found.kind}: {found.quote!r} - {found.explanation}")
    usage = result.usage
    print(
        f"steps: {result.steps}  cost: ${result.cost_usd:.4f}  "
        f"tokens in/out: {usage.input_tokens}/{usage.output_tokens}  "
        f"cache write/read: {usage.cache_creation_input_tokens}/{usage.cache_read_input_tokens}"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Answer one questionnaire question.")
    parser.add_argument("question")
    parser.add_argument("--record", type=Path, help="write the model's turns to this file")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args.question, args.record)))


if __name__ == "__main__":
    main()
