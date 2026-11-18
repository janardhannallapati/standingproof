"""A case's world on a real database: the real tools, reading a GitHub and AWS that are not."""

import inspect
import json
from pathlib import Path

import httpx2
import pytest
from sqlalchemy import func, select

import evals
from evals.cases import load_drafter
from evals.world import World, build_world
from standingproof.agent.tools import ToolOutcome
from standingproof.agent.types import ToolCall
from standingproof.models import Evidence, LibraryAnswer, PolicyDoc
from tests.conftest import Db
from tests.fakes import FakeEmbedder

CASES = load_drafter()
# What a case calls a reading, and the fact the product's check reports for it.
FACT: dict[object, str] = {
    "required": "two_factor_required",
    "protected": "protected",
    "reviews": "required_approving_reviews",
    "admins": "applies_to_administrators",
    ("github.secret_scanning", "enabled"): "secret_scanning_enabled",
    ("aws.root_mfa", "enabled"): "root_mfa_enabled",
    "set": "policy_set",
    "minimum_length": "minimum_length",
    "max_age_days": "max_age_days",
    "blocked": "all_public_access_blocked",
    "encrypted": "ebs_encrypted_by_default",
}


def world_of(db: Db, name: str, variant: str = "full") -> World:
    world = db.run(
        build_world(
            db.sessions, CASES, CASES.by_name[name], embedder=FakeEmbedder(), variant=variant
        )  # type: ignore[arg-type]
    )
    db.closing(world.aclose)
    return world


def look(db: Db, world: World, check: str, scope: str) -> ToolOutcome:
    call = ToolCall(id="t1", name="check_live_setting", arguments={"check": check, "scope": scope})
    return db.run(world.registry.execute(call))


def test_the_check_tool_reads_what_the_case_says_github_reads(db: Db) -> None:
    world = world_of(db, "b-reviews-two-vs-one")

    outcome = look(db, world, "github.branch_protection", "harborlight/app")

    seen = json.loads(outcome.content)
    assert "1 approving review " in seen["statement"]
    assert seen["facts"]["required_approving_reviews"] == 1
    assert list(outcome.sources) == [seen["id"]]


def test_the_library_of_the_case_is_in_the_database_and_searchable(db: Db) -> None:
    world = world_of(db, "b-reviews-two-vs-one")
    library = CASES.library("claims")

    async def counts() -> tuple[int, int]:
        async with db.sessions() as session:
            answers = await session.scalar(select(func.count()).select_from(LibraryAnswer))
            policies = await session.scalar(select(func.count()).select_from(PolicyDoc))
            return answers or 0, policies or 0

    assert db.run(counts()) == (len(library.answers), len(library.policies))
    call = ToolCall(
        id="t1", name="search_approved_answers", arguments={"query": "approving reviews"}
    )
    assert "two approving reviews" in db.run(world.registry.execute(call)).content


def test_a_second_world_replaces_the_first(db: Db) -> None:
    world_of(db, "b-reviews-two-vs-one")
    world = world_of(db, "c-branch-forbidden")

    async def answers() -> int:
        async with db.sessions() as session:
            return await session.scalar(select(func.count()).select_from(LibraryAnswer)) or 0

    assert db.run(answers()) == len(CASES.library("core").answers)
    assert look(db, world, "github.branch_protection", "harborlight/app").sources == {}


@pytest.mark.parametrize(
    ("name", "check", "scope", "reason"),
    [
        ("c-branch-forbidden", "github.branch_protection", "harborlight/app", "forbidden"),
        (
            "c-secret-scanning-unreachable",
            "github.secret_scanning",
            "harborlight/app",
            "unreachable",
        ),
        ("c-s3-rate-limited", "aws.s3_public_access_block", "111122223333", "rate_limited"),
    ],
)
def test_a_look_that_fails_observes_nothing(
    db: Db, name: str, check: str, scope: str, reason: str
) -> None:
    outcome = look(db, world_of(db, name), check, scope)

    assert outcome.content.startswith("Not observed:") and reason in outcome.content
    assert outcome.sources == {}


def test_a_scope_the_company_has_not_connected_is_refused(db: Db) -> None:
    world = world_of(db, "c-repo-not-connected")

    outcome = look(db, world, "github.branch_protection", "partnerco/widgets")

    assert outcome.is_error and "scope_not_connected" in outcome.content


def test_without_the_check_tool_only_the_library_is_offered(db: Db) -> None:
    world = world_of(db, "b-reviews-two-vs-one", "docs_only")

    assert [tool["name"] for tool in world.registry.definitions()] == [
        "get_policy_section",
        "search_approved_answers",
    ]


def test_every_setting_of_every_case_reads_as_its_case_says(db: Db) -> None:
    for case in CASES.cases:
        if not case.live:
            continue
        world = world_of(db, case.name)
        for live in case.live:
            outcome = look(db, world, live.check, live.scope)
            observed = bool(outcome.sources)
            assert observed == (live.reads is not None and case.allowed(live)), case.name
            if not observed:
                continue
            facts = json.loads(outcome.content)["facts"]
            for key, value in live.reads.items():  # type: ignore[union-attr]
                fact = FACT.get((live.check, key)) or FACT[key]
                assert facts.get(fact, None) is value or facts[fact] == value, (case.name, key)
                assert type(facts.get(fact)) is type(value), (case.name, key, facts)

    async def kinds() -> set[str]:
        async with db.sessions() as session:
            return set((await session.scalars(select(Evidence.status))).all())

    assert db.run(kinds())  # the last world left evidence behind


def test_the_eval_cannot_reach_a_real_github_or_aws(
    db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("the eval tried to build a real client")

    monkeypatch.setattr("boto3.session.Session", refuse)
    monkeypatch.setattr("standingproof.checks.service.build_backends", refuse)
    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", refuse)

    world = world_of(db, "b-root-mfa-off")

    assert "does not have" in look(db, world, "aws.root_mfa", "111122223333").content
    for module in Path(inspect.getfile(evals)).parent.glob("*.py"):
        source = module.read_text()
        assert "build_backends" not in source and "import boto3" not in source, module.name


def test_a_worlds_settings_can_change_between_two_looks(db: Db) -> None:
    case = CASES.by_name["g-reviews-switched-off"]
    assert case.redraft is not None
    world = world_of(db, case.name)

    before = json.loads(look(db, world, "github.branch_protection", "harborlight/app").content)
    world.becomes(case.redraft.live)
    after = json.loads(look(db, world, "github.branch_protection", "harborlight/app").content)

    assert before["facts"]["protected"] is True and "2 approving reviews" in before["statement"]
    assert after["facts"]["protected"] is False and "not protected" in after["statement"]
    assert before["id"] != after["id"]
