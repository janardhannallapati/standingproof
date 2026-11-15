"""Run every registered check once against your own accounts and print what each saw.

    uv run python scripts/try_checks.py --github-org acme --github-repo acme/app \\
        --aws-account 123456789012

Reads credentials the way the application does (SP_GITHUB_TOKEN, and the AWS SDK's usual
places). Read-only, like the checks themselves. Prints statements and facts only: no
credential, and nothing else from a reply. Stores nothing.
"""

import argparse
import asyncio
import json

from standingproof.checks.aws import AWS_CHECKS
from standingproof.checks.github import GITHUB_CHECKS
from standingproof.checks.outcome import CheckOutcome, not_configured
from standingproof.checks.registry import CHECKS
from standingproof.checks.service import build_backends
from standingproof.config import get_settings


async def _run(scopes: dict[str, str | None]) -> None:
    backends = build_backends(get_settings())
    try:
        for check in CHECKS.values():
            scope = scopes[check.scope_kind]
            if scope is None:
                print(f"{check.name}: skipped (no --{check.scope_kind} scope given)")
                continue
            outcome: CheckOutcome
            if check.system == "github":
                if backends.github is None:
                    outcome = not_configured("SP_GITHUB_TOKEN is not set.")
                else:
                    outcome = await GITHUB_CHECKS[check.name](backends.github, scope)
            else:
                assert backends.aws is not None  # noqa: S101 - build_backends always sets it
                outcome = await AWS_CHECKS[check.name](backends.aws, scope)
            print(f"{check.name} [{outcome.status}] {outcome.statement}")
            if outcome.facts:
                print(f"    facts: {json.dumps(outcome.facts)}")
            if outcome.reason:
                print(f"    reason: {outcome.reason}")
    finally:
        await backends.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--github-org", help="organisation name, for example acme")
    parser.add_argument("--github-repo", help="owner/repository, for example acme/app")
    parser.add_argument("--aws-account", help="12-digit account id")
    args = parser.parse_args()
    asyncio.run(
        _run({"org": args.github_org, "repo": args.github_repo, "account": args.aws_account})
    )


if __name__ == "__main__":
    main()
