"""How often the right source is found, measured with the real embedding model.

Run with: uv run pytest -m slow -s tests/eval
The numbers are printed so a change to chunking, the model or the ranking can be compared
with the run before it.
"""

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import text

from standingproof.config import Settings
from standingproof.library.embedding import Embedder, build_embedder
from standingproof.library.search import BOTH_MODES, SearchHit, SearchMode, search_library
from tests.conftest import ClientFactory, Db

_HERE = Path(__file__).parent
_K = 5
_VALID_UNTIL = (dt.datetime.now(dt.UTC).date() + dt.timedelta(days=365)).isoformat()


@dataclass(frozen=True)
class Score:
    recall_at_k: float
    mrr: float
    answered_no_answer: int  # questions with no source in the library that still got results


def _matches(hit: SearchHit, expected: str, answer_ids: dict[str, str]) -> bool:
    if expected.startswith("policy:"):
        return hit.kind == "policy" and (hit.heading_path or "").endswith(expected[7:])
    return hit.kind == "answer" and str(hit.id) == answer_ids[expected]


@pytest.mark.slow
def test_combined_search_finds_the_right_source_and_beats_either_search_alone(
    make_api: ClientFactory, db: Db
) -> None:
    embedder: Embedder = build_embedder(Settings(_env_file=None))
    api = make_api(embedder=embedder)
    library = json.loads((_HERE / "library.json").read_text())
    golden = json.loads((_HERE / "golden_set.json").read_text())

    answer_ids: dict[str, str] = {}
    for entry in library["answers"]:
        body = {k: entry[k] for k in ("question", "answer", "control_id")}
        created = api.post(
            "/library/answers",
            json={**body, "owner": "security@example.com", "expires_on": _VALID_UNTIL},
        )
        assert created.status_code == 201, created.text
        answer_ids[entry["key"]] = created.json()["id"]
    for policy in library["policies"]:
        files = {"file": (policy["name"], policy["text"].encode(), "text/markdown")}
        assert api.post("/library/policies", files=files).status_code == 201
    tenant_id = db.tenant()

    async def run(query: str, modes: tuple[SearchMode, ...]) -> list[SearchHit]:
        async with db.sessions() as session:
            return await search_library(
                session, tenant_id=tenant_id, query=query, embedder=embedder, limit=_K, modes=modes
            )

    def score(modes: tuple[SearchMode, ...]) -> Score:
        found = 0
        reciprocal = 0.0
        for item in golden["questions"]:
            hits = db.run(run(item["q"], modes))
            ranks = [
                rank
                for rank, hit in enumerate(hits, start=1)
                if any(_matches(hit, expected, answer_ids) for expected in item["expect"])
            ]
            if ranks:
                found += 1
                reciprocal += 1 / ranks[0]
        total = len(golden["questions"])
        wrongly_answered = sum(1 for q in golden["no_answer"] if db.run(run(q, modes)))
        return Score(found / total, reciprocal / total, wrongly_answered)

    async def nearest(query: str) -> float:
        cast = f"vector({embedder.spec.dimensions})"
        vector = await embedder.embed_query(query)
        async with db.sessions() as session:
            statement = text(
                f"SELECT min(d) FROM ("  # noqa: S608
                f"SELECT embedding::{cast} <=> CAST(:v AS {cast}) AS d "
                f"FROM library_answer_embeddings UNION ALL "
                f"SELECT embedding::{cast} <=> CAST(:v AS {cast}) FROM policy_chunk_embeddings) x"
            )
            return float((await session.execute(statement, {"v": str(vector)})).scalar_one())

    keyword = score(("keyword",))
    meaning = score(("meaning",))
    combined = score(BOTH_MODES)
    with_source = sorted(db.run(nearest(item["q"])) for item in golden["questions"])
    without_source = sorted(db.run(nearest(q)) for q in golden["no_answer"])

    print(f"\n{len(golden['questions'])} questions, model {embedder.spec.name}")
    print(f"{'search':<10}{'recall@5':>10}{'MRR':>8}{'no-source answered':>22}")
    for name, s in (("keyword", keyword), ("meaning", meaning), ("combined", combined)):
        answered = f"{s.answered_no_answer}/{len(golden['no_answer'])}"
        print(f"{name:<10}{s.recall_at_k:>10.2f}{s.mrr:>8.2f}{answered:>22}")
    print(
        "nearest vector distance: "
        f"with a source {with_source[0]:.3f} to {with_source[-1]:.3f} "
        f"(median {with_source[len(with_source) // 2]:.3f}); "
        f"without {without_source[0]:.3f} to {without_source[-1]:.3f}"
    )

    assert combined.recall_at_k >= 0.9
    assert combined.recall_at_k >= max(keyword.recall_at_k, meaning.recall_at_k)
