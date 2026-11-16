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


# --- A dropdown with a comments column, and recording a file as sent. ---


def dropdown_run(api: TestClient, db: Db) -> str:
    """Two questions in a sheet whose Answer column is a Yes/No dropdown beside Comments."""
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation

    from tests.test_questionnaires_api import upload
    from tests.test_runs_api import start
    from tests.workflow.test_review import drafted

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Security"
    sheet.append(["ID", "Question", "Answer", "Comments"])
    sheet.append(["Q1", "Is MFA enforced?", None, None])
    sheet.append(["Q2", "Is data encrypted at rest?", None, None])
    choice = DataValidation(type="list", formula1='"Yes,No,N/A"', allow_blank=True)
    choice.add("C2:C3")
    sheet.add_data_validation(choice)
    buffer = BytesIO()
    workbook.save(buffer)
    run_id = start(api, upload(api, buffer.getvalue()).json()["id"]).json()["id"]
    first, second = api.get(f"/runs/{run_id}/questions").json()["items"]
    drafted(db, uuid.UUID(first["id"]), answer="Yes. For all staff.")
    drafted(db, uuid.UUID(second["id"]), answer="We encrypt with AES-256.")
    for item in api.get(f"/runs/{run_id}/questions").json()["items"]:
        assert decide(api, item, "approve").status_code == 200
    return run_id


def test_a_dropdown_gets_its_value_and_the_comments_cell_the_sentence(
    api: TestClient, db: Db
) -> None:
    run_id = dropdown_run(api, db)

    preview = api.get(f"/runs/{run_id}/file/preview").json()
    file = api.post(f"/runs/{run_id}/file").json()
    got = api.get(f"/runs/{run_id}/file/{file['id']}")

    assert [(w["cell"], w["kind"], w["wording"]) for w in preview["written"]] == [
        ("C2", "choice", "Yes"),
        ("D2", "comment", "Yes. For all staff."),
        ("D3", "comment", "We encrypt with AES-256."),
    ]
    assert [(e["cell"], e["reason"], e["detail"]) for e in preview["left_empty"]] == [
        ("C3", "dropdown", "Yes, No, N/A")
    ]
    sheet = load_workbook(BytesIO(got.content))["Security"]
    assert (sheet["C2"].value, sheet["D2"].value) == ("Yes", "Yes. For all staff.")
    assert (sheet["C3"].value, sheet["D3"].value) == (None, "We encrypt with AES-256.")
    assert len(sheet.data_validations.dataValidation) == 1


def test_a_file_is_recorded_as_sent_once_per_customer(api: TestClient, db: Db) -> None:
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    url = f"/runs/{run_id}/file/{file_id}/sent"

    first = api.post(url, json={"customer": "Acme Corp"})
    again = api.post(url, json={"customer": "  acme corp "})
    other = api.post(url, json={"customer": "Globex"})

    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 201)
    assert first.json()["id"] == again.json()["id"]
    body = first.json()
    assert (body["customer"], body["sent_by"], body["answers"]) == ("Acme Corp", "dev-reviewer", 2)
    sent = api.get(f"/runs/{run_id}/file/preview").json()["sent"]
    assert [(s["customer"], s["answers"], s["current"]) for s in sent] == [
        ("Acme Corp", 2, True),
        ("Globex", 2, True),
    ]


def test_the_sent_record_keeps_a_hash_of_each_wording_that_went_out(
    api: TestClient, db: Db
) -> None:
    from standingproof.models import SentFile

    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    api.post(f"/runs/{run_id}/file/{file_id}/sent", json={"customer": "Acme Corp"})

    async def stored() -> SentFile:
        async with db.sessions() as session:
            return (await session.execute(select(SentFile))).scalar_one()

    record = db.run(stored())

    assert [a["position"] for a in record.answers] == [1, 2]
    assert record.answers[0]["wording_sha256"] == hashlib.sha256(b"Yes, for all staff.").hexdigest()
    assert all(len(a["question_id"]) == 36 for a in record.answers)


def test_sent_is_refused_when_it_should_be(
    api: TestClient, db: Db, make_api: ClientFactory
) -> None:
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    url = f"/runs/{run_id}/file/{file_id}/sent"

    assert api.post(url, json={"customer": "   "}).status_code == 422
    assert api.post(url, json={"customer": "x" * 201}).status_code == 422
    assert api.post(url, json={"customer": "Acme", "extra": 1}).status_code == 422
    assert (
        api.post(f"/runs/{run_id}/file/{uuid.uuid4()}/sent", json={"customer": "Acme"}).status_code
        == 404
    )


def test_a_file_made_before_a_decision_changed_cannot_be_recorded_as_sent(
    api: TestClient, db: Db
) -> None:
    run_id = decided_run(api, db)
    file_id = api.post(f"/runs/{run_id}/file").json()["id"]
    third = api.get(f"/runs/{run_id}/questions").json()["items"][2]

    async def reopen() -> None:
        async with db.sessions() as session:
            await session.execute(
                update(RunQuestion)
                .where(RunQuestion.id == uuid.UUID(third["id"]))
                .values(review="expired")
            )
            await session.commit()

    db.run(reopen())

    refused = api.post(f"/runs/{run_id}/file/{file_id}/sent", json={"customer": "Acme"})

    assert refused.status_code == 409
    assert api.get(f"/runs/{run_id}/file/preview").json()["sent"] == []
