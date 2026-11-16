"""Write wording into cells of a workbook by changing only those cells' bytes.

An .xlsx file is a zip archive of XML files, one per sheet. Loading the workbook with a
spreadsheet library and saving it again rewrites every file and drops whatever the library
does not model. Here every member of the archive is copied as it is, and inside a sheet
only the bytes of the written cells are replaced or added.

`verify_unchanged` then compares the result with the upload, using its own reading of both
files and not the writer's bookkeeping. A result that fails is never released.
"""

import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from xml.sax.saxutils import escape

from defusedxml import ElementTree

from standingproof.writeback.plan import Write
from standingproof.writeback.sheetxml import Row, SheetIndex, cell_position, scan

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_WORKBOOK = "xl/workbook.xml"
_WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"


class WritebackError(Exception):
    """The workbook could not be written to safely. Nothing was produced."""


class NotIdenticalError(WritebackError):
    """The result differs from the upload somewhere other than the written cells."""


def sheet_members(content: bytes) -> dict[str, str]:
    """Sheet name -> the archive member that holds it."""
    with zipfile.ZipFile(BytesIO(content)) as archive:
        workbook = ElementTree.fromstring(archive.read(_WORKBOOK))
        relations = ElementTree.fromstring(archive.read(_WORKBOOK_RELS))
    targets = {rel.get("Id"): rel.get("Target", "") for rel in relations.iter(_PACKAGE_REL)}
    members: dict[str, str] = {}
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        target = targets.get(sheet.get(_REL), "")
        if target:
            # Targets are relative to xl/ unless they start at the archive's root.
            members[sheet.get("name", "")] = (
                target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            )
    return members


def read_indexes(content: bytes, sheets: Iterable[str]) -> dict[str, SheetIndex]:
    """What the plan needs to know about each named sheet. Unknown names are left out."""
    members = sheet_members(content)
    with zipfile.ZipFile(BytesIO(content)) as archive:
        return {name: scan(archive.read(members[name])) for name in set(sheets) if name in members}


def _escaped(wording: str) -> str:
    """Wording as the text of an XML element. Line ends become plain newlines."""
    return escape(wording.replace("\r\n", "\n").replace("\r", "\n"))


def _cell(prefix: str, ref: str, style: str | None, wording: str) -> bytes:
    """One cell holding text. An inline string: the shared string table is not touched,
    and the text can never be read as a formula."""
    cell_position(ref)  # raises unless it is a real cell: it goes into an attribute
    if style is not None and not (style.isascii() and style.isdigit()):
        raise WritebackError("a cell's style is not a number")
    styled = f' s="{style}"' if style is not None else ""
    return (
        f'<{prefix}c r="{ref}"{styled} t="inlineStr"><{prefix}is>'
        f'<{prefix}t xml:space="preserve">{_escaped(wording)}</{prefix}t>'
        f"</{prefix}is></{prefix}c>"
    ).encode()


def _open_tag(xml: bytes, start: int, tag_end: int) -> bytes:
    """A self-closing tag's bytes, reopened: `<row r="3"/>` -> `<row r="3">`."""
    return xml[start : tag_end - 2].rstrip() + b">"


# A change to a sheet: replace xml[start:end] with new bytes. `order` sorts changes that
# start at the same place (new cells of one row, in column order).
_Splice = tuple[int, int, tuple[int, int], bytes]


def _row_splices(xml: bytes, prefix: str, row: Row, cells: Mapping[str, str]) -> list[_Splice]:
    splices: list[_Splice] = []
    existing = {cell.ref.upper(): cell for cell in row.cells}
    added: list[tuple[int, bytes]] = []
    for ref, wording in cells.items():
        column, _ = cell_position(ref)
        found = existing.get(ref)
        if found is not None:
            new = _cell(prefix, ref, found.style, wording)
            splices.append((found.start, found.end, (row.number, column), new))
        else:
            added.append((column, _cell(prefix, ref, None, wording)))
    if not added:
        return splices
    if row.self_closing:
        inside = b"".join(new for _, new in sorted(added))
        reopened = _open_tag(xml, row.start, row.tag_end) + inside + f"</{prefix}row>".encode()
        return [*splices, (row.start, row.end, (row.number, 0), reopened)]
    for column, new in added:
        later = [cell for cell in row.cells if cell_position(cell.ref)[0] > column]
        at = later[0].start if later else row.content_end
        splices.append((at, at, (row.number, column), new))
    return splices


def _write_sheet(xml: bytes, cells: Mapping[str, str]) -> bytes:
    index = scan(xml)
    prefix = index.prefix
    by_row: dict[int, dict[str, str]] = defaultdict(dict)
    for ref, wording in cells.items():
        by_row[cell_position(ref)[1]][ref.upper()] = wording

    splices: list[_Splice] = []
    new_rows: list[tuple[int, bytes]] = []
    for number, row_cells in by_row.items():
        row = index.row(number)
        if row is not None:
            splices += _row_splices(xml, prefix, row, row_cells)
            continue
        ordered = sorted(row_cells.items(), key=lambda pair: cell_position(pair[0])[0])
        inside = b"".join(_cell(prefix, ref, None, wording) for ref, wording in ordered)
        new_rows.append(
            (number, f'<{prefix}row r="{number}">'.encode() + inside + f"</{prefix}row>".encode())
        )

    if new_rows and index.data_self_closing:
        whole = (
            _open_tag(xml, index.data_start, index.data_tag_end)
            + b"".join(new for _, new in sorted(new_rows))
            + f"</{prefix}sheetData>".encode()
        )
        splices.append((index.data_start, index.data_end, (0, 0), whole))
    else:
        for number, new in new_rows:
            later = [row for row in index.rows if row.number > number]
            at = later[0].start if later else index.data_content_end
            splices.append((at, at, (number, 0), new))

    out: list[bytes] = []
    position = 0
    for start, end, _, new in sorted(splices, key=lambda splice: (splice[0], splice[2])):
        out += [xml[position:start], new]
        position = end
    out.append(xml[position:])
    return b"".join(out)


def _copy_of(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    """The same name, time and attributes, so that building twice gives the same bytes."""
    copy = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
    copy.compress_type = info.compress_type
    copy.external_attr = info.external_attr
    copy.create_system = info.create_system
    copy.comment = info.comment
    copy.extra = info.extra
    return copy


def write_cells(content: bytes, writes: Sequence[Write]) -> bytes:
    """Return the workbook with each write's wording in its cell and nothing else changed."""
    members = sheet_members(content)
    by_member: dict[str, dict[str, str]] = defaultdict(dict)
    for write in writes:
        if write.sheet not in members:
            raise WritebackError(f"sheet {write.sheet!r} is not in the workbook")
        by_member[members[write.sheet]][write.cell] = write.wording

    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(content)) as source, zipfile.ZipFile(buffer, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename in by_member:
                data = _write_sheet(data, by_member[info.filename])
            target.writestr(_copy_of(info), data)
    return buffer.getvalue()


def _same_but(original: bytes, written: bytes, cells: set[str], member: str) -> None:
    """Raise unless `written` is `original` with only `cells` filled in."""
    before, after = scan(original), scan(written)

    def differ(what: str) -> NotIdenticalError:
        return NotIdenticalError(f"{member}: {what}")

    if original[: before.data_start] != written[: after.data_start]:
        raise differ("something before the rows changed")
    if original[before.data_end :] != written[after.data_end :]:
        raise differ("something after the rows changed")

    rows_after = {row.number: row for row in after.rows}
    for row in before.rows:
        twin = rows_after.pop(row.number, None)
        if twin is None:
            raise differ(f"row {row.number} is gone")
        opening = original[row.start : row.tag_end].rstrip(b"/>").rstrip()
        if opening != written[twin.start : twin.tag_end].rstrip(b"/>").rstrip():
            raise differ(f"row {row.number} itself changed")
        kept = [written[c.start : c.end] for c in twin.cells if c.ref.upper() not in cells]
        if [original[c.start : c.end] for c in row.cells if c.ref.upper() not in cells] != kept:
            raise differ(f"another cell in row {row.number} changed")
    for number, row in rows_after.items():
        if any(cell.ref.upper() not in cells for cell in row.cells):
            raise differ(f"row {number} was added with a cell that was not asked for")

    filled = {c.ref.upper() for row in after.rows for c in row.cells if not c.empty}
    if not cells <= filled:
        raise differ("a cell that should have been written is empty")


def verify_unchanged(original: bytes, written: bytes, writes: Sequence[Write]) -> None:
    """Raise NotIdenticalError unless the two archives differ only in the written cells."""
    members = sheet_members(original)
    targets: dict[str, set[str]] = defaultdict(set)
    for write in writes:
        targets[members[write.sheet]].add(write.cell.upper())

    with zipfile.ZipFile(BytesIO(original)) as before, zipfile.ZipFile(BytesIO(written)) as after:
        names = [info.filename for info in before.infolist()]
        if names != [info.filename for info in after.infolist()]:
            raise NotIdenticalError("the archive's list of files changed")
        for name in names:
            old, new = before.read(name), after.read(name)
            if name in targets:
                _same_but(old, new, targets[name], name)
            elif old != new:
                raise NotIdenticalError(f"{name}: changed, and nothing was written to it")
