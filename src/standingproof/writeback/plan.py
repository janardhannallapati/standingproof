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
_CLOSED_REVIEWS = ("rejected", "expired")
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


@dataclass(frozen=True)
class Write:
    position: int
    sheet: str
    cell: str
    wording: str


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


def _decide(
    item: Item, sheets: Mapping[str, SheetIndex], taken: set[tuple[str, str]]
) -> Write | Skip:
    def skip(reason: str, detail: str = "", cell: str | None = item.cell) -> Skip:
        return Skip(item.position, item.sheet, cell, reason, detail)

    if item.review in _CLOSED_REVIEWS:
        return skip(str(item.review))
    if item.review not in _WRITTEN_REVIEWS:
        return skip("undecided")
    wording = item.wording or ""
    if not wording.strip():
        return skip("no_answer")
    if item.cell is None:
        return skip("no_answer_cell")
    sheet = sheets.get(item.sheet)
    if sheet is None:
        return skip("sheet_missing")

    ref = item.cell.replace("$", "").upper()
    area = sheet.merged_range(ref)
    # A merged range shows and stores only its first cell.
    target = _first_of(area) if area else ref
    if _occupied(sheet, target) or (item.sheet, target) in taken:
        return skip("cell_not_empty", cell=target)
    if len(wording) > MAX_CELL_CHARS:
        return skip("too_long", f"{len(wording)} characters", cell=target)
    if not _carriable(wording):
        return skip("bad_character", cell=target)
    choices = sheet.choices(target)
    if choices is not None and wording.strip().casefold() not in {
        choice.casefold() for choice in choices
    }:
        return skip("dropdown", ", ".join(choices), cell=target)
    return Write(item.position, item.sheet, target, wording)


def plan_writeback(items: Sequence[Item], sheets: Mapping[str, SheetIndex]) -> Plan:
    writes: list[Write] = []
    skips: list[Skip] = []
    taken: set[tuple[str, str]] = set()
    for item in sorted(items, key=lambda each: each.position):
        decided = _decide(item, sheets, taken)
        if isinstance(decided, Write):
            writes.append(decided)
            taken.add((decided.sheet, decided.cell))
        else:
            skips.append(decided)
    return Plan(writes=writes, skips=skips)
