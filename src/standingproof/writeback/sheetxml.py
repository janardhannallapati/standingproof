"""Where things are in one sheet's XML, by byte position.

A sheet is one XML file inside the workbook's archive. To change a cell and nothing else,
the writer needs the exact bytes each row and cell occupies. The XML parser reports the
byte position of every tag as it reads, so no pattern matching runs over uploaded text.

The file is untrusted: a doctype or an entity declaration is refused before anything is
expanded.
"""

from dataclasses import dataclass, field
from xml.parsers import expat


class UnsafeXmlError(Exception):
    """The sheet's XML cannot be read safely, or is not shaped like a sheet."""


@dataclass
class Cell:
    ref: str
    start: int
    end: int = 0
    style: str | None = None
    # No value, formula or inline text inside it.
    empty: bool = True


@dataclass
class Row:
    number: int
    start: int
    # The end of the opening tag, and of the whole element.
    tag_end: int
    end: int = 0
    # Where a new last cell would go: the start of the closing tag.
    content_end: int = 0
    self_closing: bool = False
    cells: list[Cell] = field(default_factory=list)


@dataclass
class ListValidation:
    ranges: list[str]
    # The typed values of the dropdown, or nothing when it points at other cells.
    values: list[str]


@dataclass
class SheetIndex:
    # "x:" when the file writes <x:row>; usually "".
    prefix: str = ""
    data_start: int = -1
    data_tag_end: int = 0
    data_content_end: int = 0
    data_end: int = 0
    data_self_closing: bool = False
    rows: list[Row] = field(default_factory=list)
    merged: list[str] = field(default_factory=list)
    validations: list[ListValidation] = field(default_factory=list)
    protected: bool = False
    _by_number: dict[int, Row] = field(default_factory=dict, repr=False)

    def row(self, number: int) -> Row | None:
        if len(self._by_number) != len(self.rows):
            self._by_number = {row.number: row for row in self.rows}
        return self._by_number.get(number)

    def merged_range(self, ref: str) -> str | None:
        return next((area for area in self.merged if in_range(ref, area)), None)

    def choices(self, ref: str) -> list[str] | None:
        """The dropdown's values for a cell: None if it has no dropdown, [] if unreadable."""
        for validation in self.validations:
            if any(in_range(ref, area) for area in validation.ranges):
                return validation.values
        return None


# The last column (XFD) and row a sheet can have, and how many ranges we will read.
MAX_COLUMN = 16_384
MAX_ROW = 1_048_576
_MAX_RANGES = 50_000
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS = "0123456789"


def cell_position(ref: str) -> tuple[int, int]:
    """("C5") -> (3, 5). Dollar signs of an absolute reference are allowed.

    Strict, because the reference comes from an uploaded file: one to three letters, then
    one to seven digits, inside the size of a real sheet. Anything else is refused.
    """
    text = ref.upper()
    position = 1 if text.startswith("$") else 0
    start = position
    while position < len(text) and text[position] in _LETTERS:
        position += 1
    letters = text[start:position]
    if position < len(text) and text[position] == "$":
        position += 1
    digits = text[position:]
    if not 1 <= len(letters) <= 3 or not 1 <= len(digits) <= 7:
        raise UnsafeXmlError("a cell reference is not a column and a row")
    if any(digit not in _DIGITS for digit in digits):
        raise UnsafeXmlError("a cell reference is not a column and a row")
    column = 0
    for letter in letters:
        column = column * 26 + _LETTERS.index(letter) + 1
    row = int(digits)
    if column > MAX_COLUMN or not 1 <= row <= MAX_ROW:
        raise UnsafeXmlError("a cell reference is outside the sheet")
    return column, row


def _row_number(text: str) -> int:
    if not 1 <= len(text) <= 7 or any(digit not in _DIGITS for digit in text):
        raise UnsafeXmlError("a row number is not a number")
    number = int(text)
    if not 1 <= number <= MAX_ROW:
        raise UnsafeXmlError("a row number is outside the sheet")
    return number


def _checked_range(area: str) -> str:
    """The range as given, after making sure both of its corners are real cells."""
    first, _, last = area.partition(":")
    cell_position(first)
    cell_position(last or first)
    return area


def in_range(ref: str, area: str) -> bool:
    first, _, last = area.partition(":")
    column, row = cell_position(ref)
    left, top = cell_position(first)
    right, bottom = cell_position(last or first)
    return left <= column <= right and top <= row <= bottom


def _tag_end(xml: bytes, start: int) -> int:
    """The position just past the `>` that closes the tag starting at `start`."""
    quote = 0
    for position in range(start, len(xml)):
        byte = xml[position]
        if quote:
            if byte == quote:
                quote = 0
        elif byte in (0x22, 0x27):  # " or '
            quote = byte
        elif byte == 0x3E:  # >
            return position + 1
    raise UnsafeXmlError("a tag is not closed")


def _typed_values(formula: str) -> list[str]:
    """`"Yes,No"` -> ["Yes", "No"]. Anything else (a range, a name) is not typed values."""
    text = formula.strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return []
    return [value.strip() for value in text[1:-1].split(",") if value.strip()]


class _Reader:
    def __init__(self, xml: bytes) -> None:
        self.xml = xml
        self.index = SheetIndex()
        self.parser = expat.ParserCreate()
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.text
        self.parser.StartDoctypeDeclHandler = self.refuse
        self.parser.EntityDeclHandler = self.refuse
        self.row: Row | None = None
        self.cell: Cell | None = None
        self.validation: ListValidation | None = None
        self.formula: list[str] | None = None
        self.ranges = 0

    def ends_empty(self, tag_end: int) -> bool:
        """True if the tag that ends at `tag_end` is self-closing. Looks at two bytes only."""
        return self.xml[tag_end - 2 : tag_end] == b"/>"

    def count_range(self) -> None:
        self.ranges += 1
        if self.ranges > _MAX_RANGES:
            raise UnsafeXmlError("the sheet has more merged or dropdown ranges than we read")

    def refuse(self, *_: object) -> None:
        raise UnsafeXmlError("the sheet declares a doctype or an entity")

    def local(self, name: str) -> str | None:
        prefix = self.index.prefix
        if self.index.data_start < 0 and name.endswith("sheetData"):
            self.index.prefix = prefix = name[: -len("sheetData")]
        return name[len(prefix) :] if name.startswith(prefix) else None

    def start(self, name: str, attributes: dict[str, str]) -> None:
        local = self.local(name)
        position = self.parser.CurrentByteIndex
        if local == "sheetData":
            self.index.data_start = position
            self.index.data_tag_end = _tag_end(self.xml, position)
            self.index.data_self_closing = self.ends_empty(self.index.data_tag_end)
        elif local == "row" and "r" in attributes:
            tag_end = _tag_end(self.xml, position)
            self.row = Row(
                number=_row_number(attributes["r"]),
                start=position,
                tag_end=tag_end,
                self_closing=self.ends_empty(tag_end),
            )
        elif local == "c" and self.row is not None and "r" in attributes:
            cell_position(attributes["r"])  # refused here if it is not a real cell
            self.cell = Cell(ref=attributes["r"], start=position, style=attributes.get("s"))
        elif local in ("v", "f", "is") and self.cell is not None:
            self.cell.empty = False
        elif local == "mergeCell" and "ref" in attributes:
            self.count_range()
            self.index.merged.append(_checked_range(attributes["ref"]))
        elif local == "sheetProtection":
            self.index.protected = attributes.get("sheet") in ("1", "true")
        elif local == "dataValidation" and attributes.get("type") == "list":
            ranges = [_checked_range(area) for area in attributes.get("sqref", "").split()]
            for _ in ranges:
                self.count_range()
            self.validation = ListValidation(ranges=ranges, values=[])
        elif local == "formula1" and self.validation is not None:
            self.formula = []

    def text(self, data: str) -> None:
        if self.formula is not None:
            self.formula.append(data)

    def closed(self, position: int, self_closing: bool) -> int:
        """Where an element ends, given where its end was reported."""
        return position if self_closing else _tag_end(self.xml, position)

    def end(self, name: str) -> None:
        local = self.local(name)
        position = self.parser.CurrentByteIndex
        if local == "sheetData":
            self.index.data_content_end = position
            self.index.data_end = self.closed(position, self.index.data_self_closing)
        elif local == "row" and self.row is not None:
            self.row.content_end = position
            self.row.end = self.closed(position, self.row.self_closing)
            self.index.rows.append(self.row)
            self.row = None
        elif local == "c" and self.cell is not None and self.row is not None:
            # A self-closing element's end is reported at the position just past it.
            self_closing = _tag_end(self.xml, self.cell.start) == position
            self.cell.end = self.closed(position, self_closing)
            self.row.cells.append(self.cell)
            self.cell = None
        elif local == "formula1" and self.validation is not None and self.formula is not None:
            self.validation.values = _typed_values("".join(self.formula))
            self.formula = None
        elif local == "dataValidation" and self.validation is not None:
            self.index.validations.append(self.validation)
            self.validation = None


def scan(xml: bytes) -> SheetIndex:
    """Read one sheet's XML and return where its rows and cells are."""
    reader = _Reader(xml)
    try:
        reader.parser.Parse(xml, True)
    except expat.ExpatError as error:
        raise UnsafeXmlError("the sheet's XML could not be read") from error
    if reader.index.data_start < 0:
        raise UnsafeXmlError("the sheet has no sheetData element")
    return reader.index
