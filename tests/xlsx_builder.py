"""Builds small workbooks in memory, so no binary fixture files are needed."""

import zipfile
from collections.abc import Mapping, Sequence
from io import BytesIO

from openpyxl import Workbook

Rows = Sequence[Sequence[object]]


def xlsx(
    sheets: Mapping[str, Rows],
    *,
    hidden: Sequence[str] = (),
    text_cells: Sequence[tuple[str, str]] = (),
) -> bytes:
    """Return the bytes of a workbook with the given sheets.

    `text_cells` lists (sheet, cell) pairs to store as plain strings even when the text
    starts with "=", the way a hostile file would carry a formula-looking string.
    """
    workbook = Workbook()
    workbook.remove(workbook.active)  # type: ignore[arg-type]
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(list(row))
        if name in hidden:
            sheet.sheet_state = "hidden"
    for sheet_name, cell in text_cells:
        workbook[sheet_name][cell].data_type = "s"
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def simple(questions: Sequence[str], *, sheet: str = "Security") -> bytes:
    rows: list[list[object]] = [["ID", "Question", "Answer"]]
    rows += [[f"Q{i}", text, None] for i, text in enumerate(questions, start=1)]
    return xlsx({sheet: rows})


def zip_with(entries: Mapping[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def rewritten(content: bytes, replace: Mapping[str, bytes]) -> bytes:
    """Copy an archive, replacing or adding the named entries."""
    with zipfile.ZipFile(BytesIO(content)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    entries.update(replace)
    return zip_with(entries)
