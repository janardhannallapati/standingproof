"""Eight workbooks shaped like real questionnaires: written, checked, and read back."""

import zipfile
from io import BytesIO

import pytest
from openpyxl import load_workbook

from standingproof.writeback.plan import Item, plan_writeback
from standingproof.writeback.writer import read_indexes, verify_unchanged, write_cells
from tests.writeback.awkward import AWKWARD, QUESTIONS

ANSWERS = ["Yes, for all staff.", "Yes. AES-256.\nKeys are rotated every year.", "Every quarter."]
ITEMS = [
    Item(position=n, sheet="Security", cell=f"C{n + 1}", review="approved", wording=answer)
    for n, answer in enumerate(ANSWERS, start=1)
]


def facts(content: bytes) -> dict[str, object]:
    """Everything about a workbook that writing answers must leave alone."""
    workbook = load_workbook(BytesIO(content))
    described: dict[str, object] = {"sheets": [(s.title, s.sheet_state) for s in workbook]}
    for sheet in workbook:
        described[sheet.title] = {
            "merged": sorted(str(area) for area in sheet.merged_cells.ranges),
            "validations": [
                (str(v.sqref), v.formula1) for v in sheet.data_validations.dataValidation
            ],
            "freeze": sheet.freeze_panes,
            "filter": sheet.auto_filter.ref,
            "protected": sheet.protection.sheet,
            "widths": {k: d.width for k, d in sheet.column_dimensions.items()},
            "conditional": [str(r.sqref) for r in sheet.conditional_formatting],
            "charts": len(sheet._charts),  # noqa: SLF001 - openpyxl has no public count
            "cells": {
                c.coordinate: (c.value, getattr(c, "style_id", None))
                for row in sheet.iter_rows()
                for c in row
                if not (sheet.title == "Security" and c.column == 3 and c.row > 1)
            },
        }
    return described


@pytest.mark.parametrize("name", list(AWKWARD))
def test_answers_go_in_and_everything_else_stays(name: str) -> None:
    upload = AWKWARD[name]()
    plan = plan_writeback(ITEMS, read_indexes(upload, ["Security"]))
    assert plan.skips == []

    filled = write_cells(upload, plan.writes)
    verify_unchanged(upload, filled, plan.writes)

    sheet = load_workbook(BytesIO(filled))["Security"]
    assert [sheet[f"C{n}"].value for n in (2, 3, 4)] == ANSWERS
    assert [sheet[f"B{n}"].value for n in (2, 3, 4)] == QUESTIONS
    assert facts(filled) == facts(upload)
    with zipfile.ZipFile(BytesIO(upload)) as before, zipfile.ZipFile(BytesIO(filled)) as after:
        assert before.namelist() == after.namelist()
        assert after.testzip() is None


def test_a_merged_answer_goes_to_the_ranges_first_cell() -> None:
    upload = AWKWARD["merged answer cells"]()
    aimed_elsewhere = [Item(1, "Security", "E2", "approved", "Yes.")]

    plan = plan_writeback(aimed_elsewhere, read_indexes(upload, ["Security"]))

    assert [write.cell for write in plan.writes] == ["C2"]


def test_a_dropdown_cell_refuses_prose_and_takes_one_of_its_values() -> None:
    upload = AWKWARD["a dropdown beside the answers"]()
    indexes = read_indexes(upload, ["Security"])

    prose = plan_writeback([Item(1, "Security", "D2", "approved", "Yes, for all staff.")], indexes)
    exact = plan_writeback([Item(1, "Security", "D2", "approved", "Yes")], indexes)

    assert [(skip.reason, skip.detail) for skip in prose.skips] == [("dropdown", "Yes, No, N/A")]
    filled = write_cells(upload, exact.writes)
    verify_unchanged(upload, filled, exact.writes)
    assert load_workbook(BytesIO(filled))["Security"]["D2"].value == "Yes"


def test_a_protected_sheet_is_reported() -> None:
    assert read_indexes(AWKWARD["a protected sheet"](), ["Security"])["Security"].protected
    assert not read_indexes(AWKWARD["merged answer cells"](), ["Security"])["Security"].protected


def test_a_dropdown_with_a_comments_column_gets_both_cells_and_keeps_its_dropdown() -> None:
    upload = AWKWARD["a dropdown beside the answers"]()
    items = [
        Item(1, "Security", "D2", "approved", "Yes. For all staff.", "C2"),
        Item(2, "Security", "D3", "approved", "We encrypt with AES-256.", "C3"),
    ]

    plan = plan_writeback(items, read_indexes(upload, ["Security"]))
    filled = write_cells(upload, plan.writes)
    verify_unchanged(upload, filled, plan.writes)

    sheet = load_workbook(BytesIO(filled))["Security"]
    assert (sheet["D2"].value, sheet["C2"].value) == ("Yes", "Yes. For all staff.")
    assert (sheet["D3"].value, sheet["C3"].value) == (None, "We encrypt with AES-256.")
    assert [(s.cell, s.reason) for s in plan.skips] == [("D3", "dropdown")]
    assert len(sheet.data_validations.dataValidation) == 1
