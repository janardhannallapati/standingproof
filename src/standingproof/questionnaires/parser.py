"""Turn the bytes of an .xlsx workbook into questions.

Pure and synchronous: bytes in, plain objects out. It knows nothing about HTTP or the
database, so it is tested without either and can be run in a thread or a worker process.
Everything read from a cell is untrusted text. It is cleaned of control characters, cut to
a maximum length, and otherwise passed on exactly as written.
"""

import itertools
import unicodedata
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from pydantic import BaseModel, ConfigDict, Field

from standingproof.questionnaires.detect import ColumnMapping, detect_columns
from standingproof.questionnaires.intake import FileLimitError

_SCAN_ROWS = 30
_PREVIEW_ROWS = 8
_PREVIEW_COLUMNS = 8
_PREVIEW_CELL_CHARS = 80
_KEPT_CONTROL_CHARACTERS = "\n\t"
_COLUMN_LETTERS = r"^[A-Za-z]{1,3}$"


class MappingError(Exception):
    """A person's column mapping does not fit the workbook."""


class ManualMapping(BaseModel):
    """Columns chosen by a person when detection found none."""

    model_config = ConfigDict(extra="forbid")

    sheet: str = Field(min_length=1, max_length=255)
    header_row: int = Field(ge=1, le=10_000, description="Row holding the headings; 1 is the top.")
    question_column: str = Field(pattern=_COLUMN_LETTERS, description="Column letter, e.g. B.")
    answer_column: str | None = Field(default=None, pattern=_COLUMN_LETTERS)
    id_column: str | None = Field(default=None, pattern=_COLUMN_LETTERS)

    def as_columns(self) -> ColumnMapping:
        def index(letters: str | None) -> int | None:
            return column_index_from_string(letters.upper()) if letters else None

        return ColumnMapping(
            header_row=self.header_row,
            question_col=column_index_from_string(self.question_column.upper()),
            answer_col=index(self.answer_column),
            id_col=index(self.id_column),
        )


@dataclass(frozen=True)
class ParsedQuestion:
    sheet: str
    cell: str
    text: str
    answer_cell: str | None
    external_id: str | None
    truncated: bool


@dataclass(frozen=True)
class SheetPreview:
    """The top-left corner of a sheet, for a person choosing columns."""

    name: str
    rows: list[list[str]]


@dataclass(frozen=True)
class ParsedWorkbook:
    questions: list[ParsedQuestion] = field(default_factory=list)
    # Sheet name -> the columns that were used, as stored with the questionnaire.
    mappings: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_sheets: list[str] = field(default_factory=list)
    unmapped_sheets: list[str] = field(default_factory=list)
    previews: list[SheetPreview] = field(default_factory=list)


def clean_text(value: str) -> str:
    """Drop control characters (keeping line breaks and tabs) and surrounding space."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    kept = (
        ch for ch in value if ch in _KEPT_CONTROL_CHARACTERS or unicodedata.category(ch) != "Cc"
    )
    return "".join(kept).strip()


def _text(value: object) -> str:
    if value is None:
        return ""
    return clean_text(value if isinstance(value, str) else str(value))


def _cell_at(row: tuple[object, ...], column: int | None) -> str:
    if column is None or column > len(row):
        return ""
    return _text(row[column - 1])


def _preview(name: str, head: Sequence[tuple[object, ...]]) -> SheetPreview:
    rows: list[list[str]] = []
    for row in head[:_PREVIEW_ROWS]:
        cells = [_text(value)[:_PREVIEW_CELL_CHARS] for value in row[:_PREVIEW_COLUMNS]]
        while cells and not cells[-1]:
            cells.pop()
        rows.append(cells)
    return SheetPreview(name=name, rows=rows)


def _describe(columns: ColumnMapping) -> dict[str, Any]:
    def letter(index: int | None) -> str | None:
        return get_column_letter(index) if index else None

    return {
        "header_row": columns.header_row,
        "question_column": get_column_letter(columns.question_col),
        "answer_column": letter(columns.answer_col),
        "id_column": letter(columns.id_col),
    }


def _questions(
    sheet: str,
    rows: Iterator[tuple[object, ...]],
    columns: ColumnMapping,
    max_chars: int,
    max_rows: int,
) -> Iterator[ParsedQuestion]:
    question_letter = get_column_letter(columns.question_col)
    answer_letter = get_column_letter(columns.answer_col) if columns.answer_col else None
    for row_number, row in enumerate(rows, start=1):
        if row_number > max_rows:
            # Bounds the work per sheet. Without it, a sheet of millions of rows that hold
            # no question text would be read to the end without ever reaching another limit.
            raise FileLimitError(
                f"sheet {sheet!r} has more than {max_rows} rows; the limit is {max_rows}"
            )
        if row_number <= columns.header_row:
            continue
        text = _cell_at(row, columns.question_col)
        if not text:
            continue
        yield ParsedQuestion(
            sheet=sheet,
            cell=f"{question_letter}{row_number}",
            text=text[:max_chars],
            answer_cell=f"{answer_letter}{row_number}" if answer_letter else None,
            external_id=_cell_at(row, columns.id_col)[:255] or None,
            truncated=len(text) > max_chars,
        )


def parse_workbook(
    content: bytes,
    *,
    max_questions: int,
    max_chars: int,
    max_rows: int,
    max_sheets: int,
    mapping: ManualMapping | None = None,
) -> ParsedWorkbook:
    """Read every visible sheet and return its questions in workbook order.

    Without `mapping`, each sheet's columns are detected by rule and a sheet where nothing
    is detected is reported, not guessed at. With `mapping`, only the named sheet is read,
    using the columns a person chose.
    """
    result = ParsedWorkbook()
    with warnings.catch_warnings():
        # openpyxl warns about spreadsheet features it does not model (for example some
        # dropdown extensions). They do not affect reading cell text.
        warnings.simplefilter("ignore", UserWarning)
        # read_only streams rows instead of building the whole sheet in memory.
        # data_only returns the value a person sees in a formula cell, not the formula.
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
    try:
        # The count that matters is the parser's own. The check on the raw archive is an
        # early, cheap refusal; this one cannot disagree with what is about to be read.
        if len(workbook.sheetnames) > max_sheets:
            raise FileLimitError(
                f"the workbook has {len(workbook.sheetnames)} sheets; the limit is {max_sheets}"
            )
        if mapping is not None and mapping.sheet not in workbook.sheetnames:
            raise MappingError(f"the workbook has no sheet named {mapping.sheet!r}")

        for sheet in workbook.worksheets:
            if sheet.sheet_state != "visible":
                result.skipped_sheets.append(sheet.title)
                continue
            if mapping is not None and sheet.title != mapping.sheet:
                continue

            rows = sheet.iter_rows(values_only=True)
            head = list(itertools.islice(rows, _SCAN_ROWS))
            result.previews.append(_preview(sheet.title, head))

            columns = mapping.as_columns() if mapping else detect_columns(head)
            if columns is None:
                result.unmapped_sheets.append(sheet.title)
                continue

            result.mappings[sheet.title] = _describe(columns)
            for question in _questions(
                sheet.title, itertools.chain(head, rows), columns, max_chars, max_rows
            ):
                if len(result.questions) >= max_questions:
                    raise FileLimitError(
                        f"the workbook has more than {max_questions} questions; "
                        "split it into smaller files"
                    )
                result.questions.append(question)
    finally:
        workbook.close()
    return result
