"""The same decisions give one file, however often and however badly it is asked for."""

import asyncio
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from standingproof.models import FilledFile
from standingproof.writeback import service
from standingproof.writeback.load import Row, file_key
from tests.conftest import Db
from tests.test_files_api import decided_run, rows


def tenant_of(db: Db, run_id: str) -> uuid.UUID:
    from standingproof.models import Run

    async def find() -> uuid.UUID:
        async with db.sessions() as session:
            return (
                await session.execute(select(Run.tenant_id).where(Run.id == uuid.UUID(run_id)))
            ).scalar_one()

    return db.run(find())


async def make(db: Db, tenant_id: uuid.UUID, run_id: str) -> Any:
    async with db.sessions() as session:
        return await service.make_file(session, tenant_id, uuid.UUID(run_id), created_by="asha")


def test_two_requests_at_the_same_moment_store_one_file(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    tenant_id = tenant_of(db, run_id)

    async def both() -> list[Any]:
        return list(await asyncio.gather(*(make(db, tenant_id, run_id) for _ in range(4))))

    results = db.run(both())

    assert rows(db) == 1
    assert len({info.id for info, _ in results}) == 1
    assert len({info.sha256 for info, _ in results}) == 1
    # Exactly one of them made it; the others were told it already existed.
    assert sorted(created for _, created in results) == [False, False, False, True]


def test_a_crash_after_building_and_before_saving_stores_nothing(
    api: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = decided_run(api, db)
    tenant_id = tenant_of(db, run_id)
    build = service._build

    def builds_then_dies(*args: Any) -> Any:
        build(*args)
        raise RuntimeError("the process died here")

    monkeypatch.setattr(service, "_build", builds_then_dies)
    with pytest.raises(RuntimeError):
        db.run(make(db, tenant_id, run_id))
    assert rows(db) == 0

    monkeypatch.setattr(service, "_build", build)
    _, created = db.run(make(db, tenant_id, run_id))

    assert created is True
    assert rows(db) == 1


def test_a_result_that_fails_its_own_check_is_never_stored(
    api: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = decided_run(api, db)
    write = service.write_cells

    def writes_one_cell_too_many(content: bytes, writes: Any) -> bytes:
        from standingproof.writeback.plan import Write

        return write(content, [*writes, Write(9, "Security", "A9", "not asked for")])

    monkeypatch.setattr(service, "write_cells", writes_one_cell_too_many)

    refused = api.post(f"/runs/{run_id}/file")

    assert refused.status_code == 422
    assert "was not made" in refused.json()["detail"]
    assert rows(db) == 0


def test_the_stored_record_lists_what_was_written_and_what_was_left(
    api: TestClient, db: Db
) -> None:
    run_id = decided_run(api, db)
    api.post(f"/runs/{run_id}/file")

    async def stored() -> FilledFile:
        async with db.sessions() as session:
            return (await session.execute(select(FilledFile))).scalar_one()

    record = db.run(stored())

    assert record.written == [
        {"position": 1, "sheet": "Security", "cell": "C2", "kind": "answer"},
        {"position": 2, "sheet": "Security", "cell": "C3", "kind": "answer"},
    ]
    assert [(e["position"], e["reason"]) for e in record.left_empty] == [(3, "rejected")]
    assert (record.writer_version, record.created_by) == (2, "dev-reviewer")


def row(**changes: Any) -> Row:
    values: dict[str, Any] = {
        "question_id": uuid.UUID(int=1),
        "position": 1,
        "sheet": "Security",
        "cell": "C2",
        "question": "Is MFA enforced?",
        "status": "ready",
        "review": "approved",
        "wording": "Yes.",
        "comment": None,
        "as_decided": True,
    }
    return Row(**(values | changes))


def test_the_key_is_the_same_in_every_process_for_the_same_input() -> None:
    # A fixed input and its fixed key. If this changes, files already made would no longer
    # be found: raise WRITER_VERSION on purpose instead.
    expected = "4d1bdd21e07c778839abd4d56bb970c5a506eeb53a707fc27a65b5bf1d104c33"
    assert file_key("ab" * 32, [row()]) == expected


def test_the_key_changes_with_anything_that_changes_the_file() -> None:
    base = file_key("ab" * 32, [row()])

    assert file_key("cd" * 32, [row()]) != base
    assert file_key("ab" * 32, [row(wording="Yes, yearly.")]) != base
    assert file_key("ab" * 32, [row(review="edited")]) != base
    assert file_key("ab" * 32, [row(comment_cell="D2")]) != base
    assert file_key("ab" * 32, [row(cell="C3")]) != base
    assert file_key("ab" * 32, [row(), row(position=2, question_id=uuid.UUID(int=2))]) != base
    # What does not change the file does not change the key.
    assert file_key("ab" * 32, [row(comment="a note", question="Reworded?")]) == base
