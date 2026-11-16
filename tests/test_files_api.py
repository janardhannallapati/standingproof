"""The filled file: preview, made once, refused when it should be, downloaded."""

import hashlib
import uuid
from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import func, select, update

from standingproof.models import FilledFile, RunQuestion
from tests.conftest import ClientFactory, Db
from tests.test_review_api import decide, run_with_drafts

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def decided_run(api: TestClient, db: Db) -> str:
    """Three questions: one approved, one edited, one rejected."""
    run_id, [first, second, third] = run_with_drafts(api, db, answer="Yes, for all staff.")
    assert decide(api, first, "approve").status_code == 200
    assert (
        decide(api, second, "edit", answer="Yes. AES-256, keys rotated yearly.").status_code == 200
    )
    assert decide(api, third, "reject", comment="Ask the backup owner.").status_code == 200
    return run_id


def rows(db: Db) -> int:
    async def count() -> int:
        async with db.sessions() as session:
            return int(
                (await session.execute(select(func.count()).select_from(FilledFile))).scalar_one()
            )

    return db.run(count())


def test_the_preview_lists_what_is_written_and_what_is_left_empty(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)

    body = api.get(f"/runs/{run_id}/file/preview").json()

    assert (body["ready"], body["undecided"], body["latest"]) == (True, 0, None)
    assert [(w["position"], w["sheet"], w["cell"], w["how"]) for w in body["written"]] == [
        (1, "Security", "C2", "approved"),
        (2, "Security", "C3", "edited"),
    ]
    assert body["written"][1]["wording"] == "Yes. AES-256, keys rotated yearly."
    assert body["written"][0]["question"] == "Is MFA enforced?"
    [empty] = body["left_empty"]
    assert (empty["position"], empty["cell"], empty["reason"]) == (3, "C4", "rejected")
    assert empty["comment"] == "Ask the backup owner."
    assert rows(db) == 0  # a preview stores nothing


def test_a_file_is_not_made_while_a_question_is_undecided(api: TestClient, db: Db) -> None:
    run_id, [first, *_] = run_with_drafts(api, db)
    decide(api, first, "approve")

    preview = api.get(f"/runs/{run_id}/file/preview").json()
    made = api.post(f"/runs/{run_id}/file")

    assert (preview["ready"], preview["undecided"]) == (False, 2)
    assert made.status_code == 409
    assert "2 questions" in made.json()["detail"]
    assert rows(db) == 0


def test_the_file_has_the_approved_wording_in_its_cells(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)

    made = api.post(f"/runs/{run_id}/file")
    assert made.status_code == 201
    file = made.json()
    got = api.get(f"/runs/{run_id}/file/{file['id']}")

    assert got.status_code == 200
    assert got.headers["content-type"] == XLSX
    assert got.headers["content-disposition"] == 'attachment; filename="acme-filled.xlsx"'
    assert hashlib.sha256(got.content).hexdigest() == file["sha256"]
    assert len(got.content) == file["size_bytes"]
    sheet = load_workbook(BytesIO(got.content))["Security"]
    assert sheet["C2"].value == "Yes, for all staff."
    assert sheet["C3"].value == "Yes. AES-256, keys rotated yearly."
    assert sheet["C4"].value is None
    assert (sheet["A2"].value, sheet["B2"].value) == ("Q1", "Is MFA enforced?")


def test_asking_again_returns_the_first_file_and_says_so(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)

    first = api.post(f"/runs/{run_id}/file")
    again = api.post(f"/runs/{run_id}/file")

    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["id"] == again.json()["id"]
    assert rows(db) == 1
    latest = api.get(f"/runs/{run_id}/file/preview").json()["latest"]
    assert (latest["id"], latest["current"]) == (first.json()["id"], True)


def test_a_changed_decision_gives_a_new_file_and_keeps_the_old_one(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    first = api.post(f"/runs/{run_id}/file").json()
    third = api.get(f"/runs/{run_id}/questions").json()["items"][2]

    async def answer_it_after_all() -> None:
        async with db.sessions() as session:
            await session.execute(
                update(RunQuestion)
                .where(RunQuestion.id == uuid.UUID(third["id"]))
                .values(review="pending", version=RunQuestion.version + 1)
            )
            await session.commit()

    db.run(answer_it_after_all())
    assert api.get(f"/runs/{run_id}/file/preview").json()["latest"]["current"] is False
    now = api.get(f"/run-questions/{third['id']}").json()
    assert decide(api, now, "edit", answer="Yes, every quarter.").status_code == 200

    second = api.post(f"/runs/{run_id}/file")

    assert second.status_code == 201
    assert second.json()["id"] != first["id"]
    assert rows(db) == 2
    assert api.get(f"/runs/{run_id}/file/{first['id']}").status_code == 200


def test_wording_that_is_not_what_the_decision_recorded_is_refused(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    first = api.get(f"/runs/{run_id}/questions").json()["items"][0]

    async def change_behind_the_reviewers_back() -> None:
        async with db.sessions() as session:
            await session.execute(
                update(RunQuestion)
                .where(RunQuestion.id == uuid.UUID(first["id"]))
                .values(final_answer="Yes, and we are certified.")
            )
            await session.commit()

    db.run(change_behind_the_reviewers_back())

    made = api.post(f"/runs/{run_id}/file")

    assert made.status_code == 409
    assert "question 1" in made.json()["detail"]
    assert rows(db) == 0


def test_another_tenant_sees_none_of_it(make_api: ClientFactory, db: Db) -> None:
    api = make_api()
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    other = make_api(dev_tenant_name="someone-else")

    assert other.get(f"/runs/{run_id}/file/preview").status_code == 404
    assert other.post(f"/runs/{run_id}/file").status_code == 404
    assert other.get(f"/runs/{run_id}/file/{file_id}").status_code == 404


def test_a_run_or_file_that_does_not_exist_is_not_found(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    nothing = uuid.uuid4()

    assert api.get(f"/runs/{nothing}/file/preview").status_code == 404
    assert api.post(f"/runs/{nothing}/file").status_code == 404
    assert api.get(f"/runs/{run_id}/file/{nothing}").status_code == 404


def test_the_downloads_name_is_built_from_a_cleaned_copy_of_the_uploads() -> None:
    from standingproof.writeback.service import filled_name

    assert filled_name("acme-vendor-security-2026.xlsx") == "acme-vendor-security-2026-filled.xlsx"
    assert filled_name('..\\evil"\r\nSet-Cookie: x.XLSX') == "evilSet-Cookie x-filled.xlsx"
    assert filled_name("") == "questionnaire-filled.xlsx"
    assert filled_name("résumé.xlsx") == "rsum-filled.xlsx"
