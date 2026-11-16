"""Send the six hand-written checker cases to the real model once and save its replies.

    uv run python scripts/record_checker_cases.py

This calls the paid API: six short requests, about $0.03 in total on the default check
model. The replies are written to tests/agent/cassettes/checker/ and replayed by
tests/agent/test_recorded_checks.py from then on, at no cost.
"""

import asyncio
import sys
from pathlib import Path

import anthropic

from standingproof.agent.checker import run_check
from standingproof.agent.model import AnthropicChecker
from standingproof.agent.recording import RecordingChecker
from standingproof.config import get_settings

ROOT = Path(__file__).parent.parent
# The cases live with the tests, which are not an installed package.
sys.path.insert(0, str(ROOT))
from tests.agent.checker_cases import CASES  # noqa: E402

OUT = ROOT / "tests" / "agent" / "cassettes" / "checker"


async def main() -> int:
    settings = get_settings()
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set. Put it in .env (see .env.example).", file=sys.stderr)
        return 2
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(), timeout=60.0, max_retries=2
    )
    checker = AnthropicChecker(client, model=settings.check_model, effort=settings.check_effort)
    total = 0.0
    try:
        for case in CASES:
            recorder = RecordingChecker(checker, OUT / f"{case.name}.json")
            result, usage = await run_check(
                recorder, question=case.question, draft=case.draft, sources=case.sources
            )
            total += result.cost_usd
            mark = "as expected" if result.outcome == case.expected else "NOT AS EXPECTED"
            print(
                f"{case.name}: {result.outcome} ({mark}), {len(result.findings)} findings, "
                f"${result.cost_usd:.4f}, tokens in/out {usage.input_tokens}/{usage.output_tokens}"
            )
            for found in result.findings:
                print(f"    {found.kind}: {found.quote!r} [{found.source_id}] {found.explanation}")
    finally:
        await client.close()
    print(f"model: {settings.check_model}  calls: {len(CASES)}  total: ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
