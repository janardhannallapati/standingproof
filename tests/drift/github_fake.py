"""A GitHub whose branch protection the test can switch, for the watcher tests."""

import json
from typing import Any

import httpx2

from standingproof.agent.types import AnswerDraft, ModelTurn, ToolCall
from standingproof.checks.service import CheckBackends
from tests.agent.fakes import finishes, wants_tools
from tests.checks.test_github import client

REPO = "acme/app"


class FakeGitHub:
    def __init__(self) -> None:
        # None: the branch is not protected.
        self.reviews: int | None = 2
        self.down = False
        self.requests = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests += 1
        if self.down:
            return httpx2.Response(503)
        if request.url.path == f"/repos/{REPO}":
            return httpx2.Response(200, json={"default_branch": "main"})
        if request.url.path == f"/repos/{REPO}/branches/main/protection" and self.reviews:
            return httpx2.Response(
                200,
                json={
                    "required_pull_request_reviews": {
                        "required_approving_review_count": self.reviews
                    },
                    "enforce_admins": {"enabled": True},
                },
            )
        return httpx2.Response(404, json={"message": "Not Found"})

    def backends(self) -> CheckBackends:
        return CheckBackends(github=client(self), aws=None)


class CitesItsCheck:
    """A model that looks at branch protection, then answers with what the look said."""

    async def complete(self, *, messages: list[dict[str, Any]], **_: Any) -> ModelTurn:
        for message in reversed(messages):
            blocks = message["content"] if isinstance(message["content"], list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    seen = json.loads(block["content"])
                    return finishes(
                        AnswerDraft(
                            status="answered",
                            answer=seen["statement"],
                            citations=[seen["id"]],
                            note="",
                        )
                    )
        return wants_tools(
            ToolCall(
                id="call-1",
                name="check_live_setting",
                arguments={"check": "github.branch_protection", "scope": REPO},
            )
        )
