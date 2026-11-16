"""Where things are in one sheet's XML, by byte position. Hand-written XML, no workbook."""

import pytest

from standingproof.writeback.sheetxml import (
    UnsafeXmlError,
    cell_position,
    in_range,
    scan,
)

HEAD = b'<?xml version="1.0" encoding="UTF-8"?>\n<worksheet xmlns="http://m">'


def sheet(inner: bytes, after: bytes = b"") -> bytes:
    return HEAD + b"<sheetData>" + inner + b"</sheetData>" + after + b"</worksheet>"


def test_rows_and_cells_are_found_with_the_bytes_they_occupy() -> None:
    xml = sheet(b'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" s="3"/></row><row r="3"/>')

    index = scan(xml)

    assert [row.number for row in index.rows] == [1, 3]
    first, second = index.rows[0].cells
    assert xml[first.start : first.end] == b'<c r="A1" t="s"><v>0</v></c>'
    assert (first.ref, first.empty) == ("A1", False)
    assert xml[second.start : second.end] == b'<c r="B1" s="3"/>'
    assert (second.ref, second.empty, second.style) == ("B1", True, "3")
    assert index.rows[1].self_closing
    assert xml[index.rows[1].start : index.rows[1].end] == b'<row r="3"/>'


def test_the_place_new_content_goes_is_known_for_a_row_and_for_the_sheet() -> None:
    xml = sheet(b'<row r="1"><c r="A1"/></row>')

    index = scan(xml)

    assert xml[index.rows[0].content_end :].startswith(b"</row>")
    assert xml[index.data_content_end :].startswith(b"</sheetData>")
    assert not index.data_self_closing


def test_an_empty_sheet_can_be_a_self_closing_element() -> None:
    xml = HEAD + b"<sheetData/></worksheet>"

    index = scan(xml)

    assert index.rows == []
    assert index.data_self_closing
    assert xml[index.data_start : index.data_end] == b"<sheetData/>"


def test_a_cell_with_a_formula_or_inline_text_is_not_empty() -> None:
    xml = sheet(
        b'<row r="1"><c r="A1"><f>1+1</f></c><c r="B1" t="inlineStr"><is><t>x</t></is></c></row>'
    )

    assert [cell.empty for cell in scan(xml).rows[0].cells] == [False, False]


def test_an_attribute_value_may_contain_a_closing_bracket() -> None:
    xml = sheet(b'<row r="1" note="a>b"><c r="A1"/></row>')

    row = scan(xml).rows[0]

    assert xml[row.start : row.tag_end] == b'<row r="1" note="a>b">'


def test_merged_ranges_validations_and_protection_are_read() -> None:
    after = (
        b'<sheetProtection sheet="1"/><mergeCells count="1"><mergeCell ref="C2:D3"/></mergeCells>'
        b'<dataValidations count="2">'
        b'<dataValidation type="list" sqref="C5 C7:C9"><formula1>"Yes,No,N/A"</formula1>'
        b"</dataValidation>"
        b'<dataValidation type="list" sqref="E1"><formula1>$Z$1:$Z$3</formula1></dataValidation>'
        b"</dataValidations>"
    )

    index = scan(sheet(b"", after))

    assert index.merged == ["C2:D3"]
    assert index.protected
    assert index.choices("C8") == ["Yes", "No", "N/A"]
    assert index.choices("E1") == []  # a list we cannot read: a range, not typed values
    assert index.choices("A1") is None  # no list on this cell


def test_a_prefixed_main_namespace_is_followed() -> None:
    xml = (
        b'<x:worksheet xmlns:x="http://m"><x:sheetData><x:row r="2"><x:c r="A2"/></x:row>'
        b"</x:sheetData></x:worksheet>"
    )

    index = scan(xml)

    assert index.prefix == "x:"
    assert index.rows[0].cells[0].ref == "A2"


@pytest.mark.parametrize(
    "xml",
    [
        b'<!DOCTYPE w [<!ENTITY a "aaaa">]><worksheet><sheetData/></worksheet>',
        b"<!DOCTYPE worksheet><worksheet><sheetData/></worksheet>",
    ],
)
def test_a_doctype_or_entity_declaration_is_refused(xml: bytes) -> None:
    with pytest.raises(UnsafeXmlError):
        scan(xml)


def test_xml_with_no_sheet_data_is_refused() -> None:
    with pytest.raises(UnsafeXmlError):
        scan(b"<worksheet/>")


def test_a_cell_reference_is_split_into_column_and_row() -> None:
    assert cell_position("C5") == (3, 5)
    assert cell_position("AA10") == (27, 10)
    assert cell_position("$B$2") == (2, 2)


def test_a_cell_is_found_inside_a_range() -> None:
    assert in_range("C8", "C7:C9")
    assert in_range("D3", "C2:D3")
    assert in_range("C5", "C5")
    assert not in_range("C10", "C7:C9")
    assert not in_range("B8", "C7:C9")
