"""Which cells get which wording, and which are left empty and why.

Pure: it takes the decided questions and what is known about each sheet, and returns a
plan. Nothing here opens a file or a database. A cell that cannot be written safely is
listed with a reason; it is never guessed at, cut short or overwritten.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from standingproof.writeback.sheetxml import SheetIndex, cell_position

# The most characters one cell can hold.
MAX_CELL_CHARS = 32_767
_WRITTEN_REVIEWS = ("approved", "edited")
# "unanswered": the question failed or never started, so nobody could decide it.
_CLOSED_REVIEWS = ("rejected", "expired", "unanswered")
# Characters below a space that XML can carry.
_ALLOWED_CONTROL = "\t\n\r"
_HEX = "0123456789abcdefABCDEF"


@dataclass(frozen=True)
class Item:
    """One question of a run, as far as writing is concerned."""

    position: int
    sheet: str
    cell: str | None
    review: str | None
    wording: str | None
    # The free-text cell beside the answer cell, when the sheet has a comments column.
    comment_cell: str | None = None


@dataclass(frozen=True)
class Write:
    position: int
    sheet: str
    cell: str
    wording: str
    # answer: the approved wording in the answer cell. comment: the same wording in the
    # comments cell. choice: a dropdown's value, taken from the wording's first word.
    kind: str = "answer"


@dataclass(frozen=True)
class Skip:
    position: int
    sheet: str
    cell: str | None
    # undecided | rejected | expired | no_answer | no_answer_cell | sheet_missing |
    # cell_not_empty | dropdown | too_long | bad_character
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class Plan:
    writes: list[Write]
    skips: list[Skip]


def _first_of(area: str) -> str:
    return area.partition(":")[0].replace("$", "").upper()


def _occupied(sheet: SheetIndex, ref: str) -> bool:
    _, number = cell_position(ref)
    row = sheet.row(number)
    if row is None:
        return False
    return any(cell.ref.upper() == ref and not cell.empty for cell in row.cells)


def _reads_as_escape(wording: str) -> bool:
    """True if the text holds `_x` + four hex digits + `_`.

    A spreadsheet reads that as one escaped character (`_x000D_` is a carriage return), so
    writing it as typed would change the wording when the file is opened.
    """
    for position in range(len(wording) - 6):
        if (
            wording[position : position + 2] == "_x"
            and wording[position + 6] == "_"
            and all(digit in _HEX for digit in wording[position + 2 : position + 6])
        ):
            return True
    return False


def _carriable(wording: str) -> bool:
    plain = all(ch >= " " or ch in _ALLOWED_CONTROL for ch in wording)
    return plain and not _reads_as_escape(wording)


# What may follow a dropdown's value at the start of an answer for it to count as that value.
_AFTER_A_CHOICE = (".", ",", ";", ":", " -", " \u2013", " \u2014")


def leading_choice(wording: str, choices: Sequence[str]) -> str | None:
    """The dropdown value an approved answer begins with, in the dropdown's own spelling.

    "Yes. Keys are rotated." begins with "Yes". "Yesterday we..." and "Yes for staff" do
    not: the value must be followed by the end or by punctuation. Nothing is inferred from
    the meaning of the sentence, and when two values fit, the longer one is taken.
    """
    text = wording.strip().casefold()
    for choice in sorted(choices, key=len, reverse=True):
        value = choice.casefold()
        if not value or not text.startswith(value):
            continue
        rest = text[len(value) :]
        if not rest or rest.startswith(_AFTER_A_CHOICE):
            return choice
    return None


def _target(sheet: SheetIndex, cell: str) -> str:
    ref = cell.replace("$", "").upper()
    area = sheet.merged_range(ref)
    # A merged range shows and stores only its first cell.
    return _first_of(area) if area else ref


def _decide(
    item: Item, sheets: Mapping[str, SheetIndex], taken: set[tuple[str, str]]
) -> list[Write | Skip]:
    def skip(reason: str, detail: str = "", cell: str | None = item.cell) -> Skip:
        return Skip(item.position, item.sheet, cell, reason, detail)

    def unfit(text: str, cell: str) -> Skip | None:
        """Why this text cannot go into this cell, or None if it can."""
        if _occupied(sheet, cell) or (item.sheet, cell) in taken:
            return skip("cell_not_empty", cell=cell)
        if len(text) > MAX_CELL_CHARS:
            return skip("too_long", f"{len(text)} characters", cell=cell)
        if not _carriable(text):
            return skip("bad_character", cell=cell)
        return None

    if item.review in _CLOSED_REVIEWS:
        return [skip(str(item.review))]
    if item.review not in _WRITTEN_REVIEWS:
        return [skip("undecided")]
    wording = item.wording or ""
    if not wording.strip():
        return [skip("no_answer")]
    if item.cell is None:
        return [skip("no_answer_cell")]
    found = sheets.get(item.sheet)
    if found is None:
        return [skip("sheet_missing")]
    sheet = found

    target = _target(sheet, item.cell)
    choices = sheet.choices(target)
    fits_dropdown = choices is None or wording.strip().casefold() in {
        choice.casefold() for choice in choices
    }
    if fits_dropdown:
        problem = unfit(wording, target)
        return [problem or Write(item.position, item.sheet, target, wording)]

    # The answer cell is a dropdown and the wording is not one of its values.
    dropdown = skip("dropdown", ", ".join(choices or []), cell=target)
    if item.comment_cell is None:
        return [dropdown]
    beside = _target(sheet, item.comment_cell)
    problem = unfit(wording, beside)
    if problem is None and sheet.choices(beside) is not None:
        problem = skip("dropdown", cell=beside)
    if problem is not None:
        # Half an answer is worse than none: without the explanation, no value either.
        return [dropdown, problem]
    comment = Write(item.position, item.sheet, beside, wording, kind="comment")
    choice = leading_choice(wording, choices or [])
    if choice is None or unfit(choice, target) is not None:
        return [comment, dropdown]
    return [Write(item.position, item.sheet, target, choice, kind="choice"), comment]


def plan_writeback(items: Sequence[Item], sheets: Mapping[str, SheetIndex]) -> Plan:
    writes: list[Write] = []
    skips: list[Skip] = []
    taken: set[tuple[str, str]] = set()
    for item in sorted(items, key=lambda each: each.position):
        for decided in _decide(item, sheets, taken):
            if isinstance(decided, Write):
                writes.append(decided)
                taken.add((decided.sheet, decided.cell))
            else:
                skips.append(decided)
    return Plan(writes=writes, skips=skips)
