"""Workbooks shaped the way real questionnaires are, built in memory for the tests."""

import zipfile
from collections.abc import Callable
from io import BytesIO

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet

from tests.xlsx_builder import rewritten

QUESTIONS = ["Is MFA enforced?", "Is data encrypted at rest?", "Are backups tested?"]


def _base() -> tuple[Workbook, Worksheet]:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Security"
    sheet.append(["ID", "Question", "Answer"])
    for number, question in enumerate(QUESTIONS, start=1):
        sheet.append([f"Q{number}", question, None])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDDDDD")
    sheet.column_dimensions["B"].width = 48
    sheet.column_dimensions["C"].width = 60
    return workbook, sheet


def _saved(workbook: Workbook) -> bytes:
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def merged_answer_cells() -> bytes:
    workbook, sheet = _base()
    for row in (2, 3, 4):
        sheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=5)
    return _saved(workbook)


def styled_empty_answer_cells() -> bytes:
    workbook, sheet = _base()
    for row in (2, 3, 4):
        sheet.cell(row=row, column=3).alignment = Alignment(wrap_text=True, vertical="top")
        sheet.cell(row=row, column=3).fill = PatternFill("solid", fgColor="FFF2CC")
    return _saved(workbook)


def dropdown_beside_the_answers() -> bytes:
    workbook, sheet = _base()
    sheet["D1"] = "Status"
    choice = DataValidation(type="list", formula1='"Yes,No,N/A"', allow_blank=True)
    choice.add("D2:D4")
    sheet.add_data_validation(choice)
    return _saved(workbook)


def hidden_sheet_and_a_cover_page() -> bytes:
    workbook, _ = _base()
    cover = workbook.create_sheet("Cover", 0)
    cover["A1"] = "Vendor security questionnaire"
    lists = workbook.create_sheet("Lists")
    lists.append(["Yes"])
    lists.append(["No"])
    lists.sheet_state = "hidden"
    return _saved(workbook)


def protected_sheet() -> bytes:
    workbook, sheet = _base()
    sheet.protection.sheet = True
    sheet.protection.password = "customer"  # noqa: S105 - a test workbook's sheet lock
    return _saved(workbook)


def frozen_panes_and_a_filter() -> bytes:
    workbook, sheet = _base()
    sheet.freeze_panes = "C2"
    sheet.auto_filter.ref = "A1:C4"
    return _saved(workbook)


def conditional_formatting_and_a_chart() -> bytes:
    workbook, sheet = _base()
    sheet["E1"], sheet["E2"], sheet["E3"], sheet["E4"] = "Score", 3, 1, 2
    red = PatternFill("solid", bgColor="FFC7CE")
    sheet.conditional_formatting.add(
        "E2:E4", CellIsRule(operator="lessThan", formula=["2"], fill=red)
    )
    chart = BarChart()
    chart.add_data(Reference(sheet, min_col=5, min_row=1, max_row=4), titles_from_data=True)
    sheet.add_chart(chart, "G2")
    return _saved(workbook)


def extension_namespaces() -> bytes:
    """What Excel itself writes: ignorable extension namespaces and attributes on rows."""
    content = _saved(_base()[0])
    name = "xl/worksheets/sheet1.xml"
    with zipfile.ZipFile(BytesIO(content)) as archive:
        xml = archive.read(name)
    main = b'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    extended = (
        main
        + b' xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
        + b' mc:Ignorable="x14ac"'
        + b' xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"'
    )
    assert main in xml
    xml = xml.replace(main, extended, 1).replace(
        b'<row r="2"', b'<row r="2" x14ac:dyDescent="0.25"'
    )
    return rewritten(content, {name: xml})


AWKWARD: dict[str, Callable[[], bytes]] = {
    "merged answer cells": merged_answer_cells,
    "styled empty answer cells": styled_empty_answer_cells,
    "a dropdown beside the answers": dropdown_beside_the_answers,
    "a hidden sheet and a cover page": hidden_sheet_and_a_cover_page,
    "a protected sheet": protected_sheet,
    "frozen panes and a filter": frozen_panes_and_a_filter,
    "conditional formatting and a chart": conditional_formatting_and_a_chart,
    "extension namespaces": extension_namespaces,
}
