"""Writing wording into cells, and proving that nothing else in the workbook changed."""

import zipfile
from io import BytesIO

import pytest
from openpyxl import load_workbook

from standingproof.writeback.plan import Write
from standingproof.writeback.writer import (
    NotIdenticalError,
    WritebackError,
    read_indexes,
    sheet_members,
    verify_unchanged,
    write_cells,
)
from tests.xlsx_builder import rewritten, simple, xlsx

QUESTIONS = ["Is MFA enforced?", "Do you test backups?"]
SHEET = "xl/worksheets/sheet1.xml"


def write(cell: str, wording: str, position: int = 1) -> Write:
    return Write(position=position, sheet="Security", cell=cell, wording=wording)


def value(content: bytes, cell: str, sheet: str = "Security") -> object:
    return load_workbook(BytesIO(content))[sheet][cell].value


def member(content: bytes, name: str) -> bytes:
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return archive.read(name)


def with_sheet(content: bytes, change) -> bytes:  # type: ignore[no-untyped-def]
    return rewritten(content, {SHEET: change(member(content, SHEET))})


def test_wording_is_in_its_cell_when_the_file_is_read_back() -> None:
    upload = simple(QUESTIONS)
    writes = [write("C2", "Yes, for all staff."), write("C3", "Every quarter.", 2)]

    filled = write_cells(upload, writes)

    assert value(filled, "C2") == "Yes, for all staff."
    assert value(filled, "C3") == "Every quarter."
    assert value(filled, "B2") == "Is MFA enforced?"
    verify_unchanged(upload, filled, writes)


def test_every_other_file_in_the_archive_is_byte_for_byte_the_same() -> None:
    upload = simple(QUESTIONS)

    filled = write_cells(upload, [write("C2", "Yes.")])

    with zipfile.ZipFile(BytesIO(upload)) as before, zipfile.ZipFile(BytesIO(filled)) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name != SHEET:
                assert before.read(name) == after.read(name), name


def test_building_twice_gives_the_same_bytes() -> None:
    upload = simple(QUESTIONS)
    writes = [write("C2", "Yes.")]

    assert write_cells(upload, writes) == write_cells(upload, writes)


@pytest.mark.parametrize(
    "wording",
    [
        "=1+1",
        "+SUM(A1)",
        "-2",
        "@cmd",
        "Tom & Jerry <b> \"quoted\" 'single'",
        "Line one.\nLine two.",
        "అవును. ఉద్యోగులందరికీ MFA తప్పనిసరి.",
        "  two leading spaces",
        "an underscore_x and _x12_ that are not escapes",
    ],
)
def test_wording_is_stored_as_text_exactly_as_approved(wording: str) -> None:
    upload = simple(QUESTIONS)

    filled = write_cells(upload, [write("C2", wording)])

    cell = load_workbook(BytesIO(filled))["Security"]["C2"]
    assert cell.value == wording
    assert cell.data_type in ("s", "inlineStr", "str")


def test_windows_line_ends_become_plain_ones() -> None:
    filled = write_cells(simple(QUESTIONS), [write("C2", "One.\r\nTwo.")])

    assert value(filled, "C2") == "One.\nTwo."


def test_an_existing_empty_cell_keeps_its_style() -> None:
    upload = with_sheet(
        simple(QUESTIONS),
        lambda xml: xml.replace(b'</row><row r="3"', b'<c r="C2" s="7"/></row><row r="3"', 1),
    )
    assert b'<c r="C2" s="7"/>' in member(upload, SHEET)
    writes = [write("C2", "Yes.")]

    filled = write_cells(upload, writes)

    assert b'<c r="C2" s="7" t="inlineStr">' in member(filled, SHEET)
    verify_unchanged(upload, filled, writes)


def test_a_cell_is_added_before_the_cells_to_its_right() -> None:
    upload = xlsx(
        {"Security": [["ID", "Question", None, "Notes"], ["Q1", "Is MFA enforced?", None, "n/a"]]}
    )
    writes = [write("C2", "Yes.")]

    filled = write_cells(upload, writes)

    sheet = member(filled, SHEET)
    assert sheet.index(b'r="B2"') < sheet.index(b'r="C2"') < sheet.index(b'r="D2"')
    assert value(filled, "D2") == "n/a"
    verify_unchanged(upload, filled, writes)


def test_a_row_that_does_not_exist_yet_is_added_in_order() -> None:
    upload = xlsx({"Security": [["ID", "Question", "Answer"], ["Q1", "Is MFA enforced?", None]]})
    writes = [write("C5", "Late answer."), write("C2", "Yes.", 2)]

    filled = write_cells(upload, writes)

    sheet = member(filled, SHEET)
    assert sheet.index(b'<row r="2"') < sheet.index(b'<row r="5"')
    assert (value(filled, "C5"), value(filled, "C2")) == ("Late answer.", "Yes.")
    verify_unchanged(upload, filled, writes)


def test_a_self_closing_row_and_an_empty_sheet_are_opened_up() -> None:
    upload = with_sheet(
        simple(QUESTIONS), lambda xml: xml.replace(b"</sheetData>", b'<row r="9"/></sheetData>')
    )
    writes = [write("C9", "In a row that was empty.")]
    filled = write_cells(upload, writes)
    assert value(filled, "C9") == "In a row that was empty."
    verify_unchanged(upload, filled, writes)

    empty = with_sheet(
        xlsx({"Security": []}), lambda xml: xml.replace(b"<sheetData></sheetData>", b"<sheetData/>")
    )
    assert b"<sheetData/>" in member(empty, SHEET)
    writes = [write("B2", "Into an empty sheet.")]
    filled = write_cells(empty, writes)
    assert value(filled, "B2") == "Into an empty sheet."
    verify_unchanged(empty, filled, writes)


def test_a_sheet_is_found_by_its_name_among_several() -> None:
    upload = xlsx(
        {"Intro": [["Read me"]], "Security": [["ID", "Question", "Answer"], ["Q1", "Q?", None]]}
    )

    assert sheet_members(upload) == {
        "Intro": "xl/worksheets/sheet1.xml",
        "Security": "xl/worksheets/sheet2.xml",
    }
    filled = write_cells(upload, [write("C2", "Yes.")])
    assert value(filled, "C2") == "Yes."
    assert member(upload, "xl/worksheets/sheet1.xml") == member(filled, "xl/worksheets/sheet1.xml")
    assert set(read_indexes(upload, ["Security", "Nowhere"])) == {"Security"}


def test_a_sheet_that_is_not_in_the_workbook_is_an_error() -> None:
    with pytest.raises(WritebackError):
        write_cells(simple(QUESTIONS), [Write(1, "Nowhere", "C2", "Yes.")])


# --- The check on the result. Each of these is a change the writer never makes. ---


def filled_and_writes() -> tuple[bytes, bytes, list[Write]]:
    upload = simple(QUESTIONS)
    writes = [write("C2", "Yes.")]
    return upload, write_cells(upload, writes), writes


def test_the_check_fails_when_another_cell_changed() -> None:
    upload, filled, writes = filled_and_writes()
    tampered = with_sheet(filled, lambda xml: xml.replace(b'<c r="B2"', b'<c r="B2" s="1"', 1))
    assert tampered != filled

    with pytest.raises(NotIdenticalError, match="another cell in row"):
        verify_unchanged(upload, tampered, writes)


def test_the_check_fails_when_something_outside_the_rows_changed() -> None:
    upload, filled, writes = filled_and_writes()
    tampered = with_sheet(
        filled, lambda xml: xml.replace(b"</sheetData>", b"</sheetData><extra/>", 1)
    )

    with pytest.raises(NotIdenticalError, match="after the rows"):
        verify_unchanged(upload, tampered, writes)


def test_the_check_fails_when_another_file_in_the_archive_changed() -> None:
    upload, filled, writes = filled_and_writes()
    styles = member(filled, "xl/styles.xml")
    tampered = rewritten(filled, {"xl/styles.xml": styles + b" "})

    with pytest.raises(NotIdenticalError, match=r"xl/styles\.xml"):
        verify_unchanged(upload, tampered, writes)


def test_the_check_fails_when_a_file_was_added_or_a_row_removed() -> None:
    upload, filled, writes = filled_and_writes()

    with pytest.raises(NotIdenticalError, match="list of files"):
        verify_unchanged(upload, rewritten(filled, {"xl/extra.xml": b"<x/>"}), writes)

    def without_row_three(xml: bytes) -> bytes:
        start = xml.index(b'<row r="3"')
        return xml[:start] + xml[xml.index(b"</row>", start) + len(b"</row>") :]

    with pytest.raises(NotIdenticalError, match="row 3 is gone"):
        verify_unchanged(upload, with_sheet(filled, without_row_three), writes)


def test_the_check_fails_when_a_cell_nobody_asked_for_was_written() -> None:
    upload = simple(QUESTIONS)
    filled = write_cells(upload, [write("C2", "Yes."), write("C3", "Extra.", 2)])

    with pytest.raises(NotIdenticalError):
        verify_unchanged(upload, filled, [write("C2", "Yes.")])


def test_the_check_fails_when_the_cell_was_not_written_at_all() -> None:
    upload = simple(QUESTIONS)

    with pytest.raises(NotIdenticalError, match="should have been written"):
        verify_unchanged(upload, upload, [write("C2", "Yes.")])


def test_an_empty_cell_with_a_closing_tag_is_replaced_whole() -> None:
    before, after = b'</row><row r="3"', b'<c r="C2" s="0"></c></row><row r="3"'
    upload = with_sheet(simple(QUESTIONS), lambda xml: xml.replace(before, after, 1))
    writes = [write("C2", "Yes.")]

    filled = write_cells(upload, writes)

    assert member(filled, SHEET).count(b"</c></c>") == 0
    assert value(filled, "C2") == "Yes."
    verify_unchanged(upload, filled, writes)


def test_a_cell_reference_that_is_not_a_cell_is_never_written() -> None:
    with pytest.raises(Exception, match="cell reference"):
        write_cells(simple(QUESTIONS), [write('C2" t="e', "Yes.")])
