"""Find which row holds the column headings and which column holds the questions.

Deliberately a fixed set of rules and no model call: the result is free, instant, the same
every time, and explainable to the person who uploaded the file. When the rules find
nothing, the caller asks a person to choose the columns.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

_QUESTION_HEADINGS = frozenset(
    {
        "question",
        "questions",
        "question text",
        "security question",
        "requirement",
        "requirements",
        "requirement description",
        "control",
        "control description",
    }
)
_ID_HEADINGS = frozenset(
    {
        "id",
        "#",
        "no",
        "ref",
        "reference",
        "number",
        "item",
        "question id",
        "question #",
        "question no",
        "question number",
        "control id",
        "requirement id",
    }
)
_ANSWER_HEADINGS = frozenset({"answer", "answers", "response", "responses", "reply"})
_ANSWER_SUFFIXES = (" answer", " response")
# The free-text column beside a Yes/No dropdown. A heading must equal one of these or end
# with one of the suffixes; a sentence that merely contains "note" is not a heading.
_COMMENT_HEADINGS = frozenset(
    {
        "comment",
        "comments",
        "note",
        "notes",
        "remark",
        "remarks",
        "explanation",
        "details",
        "additional information",
    }
)
_COMMENT_SUFFIXES = (" comments", " notes", " explanation")

_NOT_WORD = re.compile(r"[^a-z0-9#]+")


@dataclass(frozen=True)
class ColumnMapping:
    """Row and column numbers, counted from 1 as a spreadsheet does."""

    header_row: int
    question_col: int
    answer_col: int | None
    id_col: int | None
    # Where the explanation goes when the answer cell is a dropdown. Often absent.
    comment_col: int | None = None


def _normalise(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _NOT_WORD.sub(" ", value.lower()).strip()


def _is_answer(heading: str) -> bool:
    return heading in _ANSWER_HEADINGS or heading.endswith(_ANSWER_SUFFIXES)


def _is_comment(heading: str) -> bool:
    return heading in _COMMENT_HEADINGS or heading.endswith(_COMMENT_SUFFIXES)


def _first(headings: list[str], matches: Callable[[str], bool]) -> int | None:
    for index, heading in enumerate(headings, start=1):
        if matches(heading):
            return index
    return None


def detect_columns(
    rows: Sequence[Sequence[object]], *, scan_rows: int = 30
) -> ColumnMapping | None:
    """Return the mapping for the first row that has a known question heading, or None.

    A heading must equal a known term once case, spacing and punctuation are ignored. A
    sentence that merely contains the word "question" is not a heading.
    """
    for row_number, row in enumerate(rows[:scan_rows], start=1):
        headings = [_normalise(cell) for cell in row]
        question_col = _first(headings, _QUESTION_HEADINGS.__contains__)
        if question_col is None:
            continue
        return ColumnMapping(
            header_row=row_number,
            question_col=question_col,
            answer_col=_first(headings, _is_answer),
            id_col=_first(headings, _ID_HEADINGS.__contains__),
            comment_col=_first(headings, _is_comment),
        )
    return None
