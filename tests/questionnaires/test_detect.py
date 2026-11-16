from standingproof.questionnaires.detect import ColumnMapping, detect_columns


def test_finds_the_header_on_the_first_row() -> None:
    rows = [["ID", "Question", "Answer"], ["Q1", "Is MFA enforced?", None]]

    assert detect_columns(rows) == ColumnMapping(
        header_row=1, question_col=2, answer_col=3, id_col=1
    )


def test_finds_the_header_under_a_title_block() -> None:
    rows = [
        ["Acme Vendor Security Questionnaire", None, None],
        ["Please answer every question. Answer honestly.", None, None],
        [None, None, None],
        ["Ref", "Requirement", "Vendor Response"],
        ["1.1", "Is data encrypted at rest?", None],
    ]

    assert detect_columns(rows) == ColumnMapping(
        header_row=4, question_col=2, answer_col=3, id_col=1
    )


def test_headings_match_without_regard_to_case_spacing_or_punctuation() -> None:
    rows = [["  QUESTION  no. ", "Control   Description:", "your RESPONSE"]]

    assert detect_columns(rows) == ColumnMapping(
        header_row=1, question_col=2, answer_col=3, id_col=1
    )


def test_a_question_number_heading_is_the_id_not_the_question() -> None:
    rows = [["Question #", "Question", "Comments"]]

    mapping = detect_columns(rows)

    assert mapping is not None
    assert (mapping.id_col, mapping.question_col, mapping.answer_col) == (1, 2, None)


def test_answer_and_id_columns_are_optional() -> None:
    assert detect_columns([["Questions"]]) == ColumnMapping(
        header_row=1, question_col=1, answer_col=None, id_col=None
    )


def test_returns_none_when_no_heading_is_known() -> None:
    rows = [["Topic", "Details", "Notes"], ["Access", "Is MFA enforced?", None]]

    assert detect_columns(rows) is None


def test_a_sentence_that_mentions_question_is_not_a_header() -> None:
    rows = [["Please answer each question below as fully as you can."], ["Is MFA enforced?"]]

    assert detect_columns(rows) is None


def test_only_the_first_rows_are_scanned() -> None:
    rows = [[None]] * 30 + [["Question"]]

    assert detect_columns(rows, scan_rows=30) is None


def test_a_comments_column_is_found_by_its_heading() -> None:
    import pytest

    for heading in [
        "Comments",
        "Notes",
        "Explanation",
        "Remarks",
        "Additional information",
        "Vendor comments",
        "Supporting notes",
    ]:
        mapping = detect_columns([["ID", "Question", "Answer", heading]])
        assert mapping is not None and mapping.comment_col == 4, heading
    for heading in ["Commentary on scope", "Noted by", "Status"]:
        mapping = detect_columns([["ID", "Question", "Answer", heading]])
        assert mapping is not None and mapping.comment_col is None, heading
    assert pytest is not None


def test_a_sheet_without_a_comments_heading_has_no_comments_column() -> None:
    mapping = detect_columns([["ID", "Question", "Answer"]])

    assert mapping is not None and mapping.comment_col is None
