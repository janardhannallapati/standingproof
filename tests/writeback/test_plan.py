"""Which cells get which wording, and which are left empty and why. Pure."""

from standingproof.writeback.plan import Item, Skip, Write, plan_writeback
from standingproof.writeback.sheetxml import scan

HEAD = b'<worksheet xmlns="http://m">'


def index(inner: bytes = b"", after: bytes = b""):  # type: ignore[no-untyped-def]
    return scan(HEAD + b"<sheetData>" + inner + b"</sheetData>" + after + b"</worksheet>")


def item(position: int = 1, cell: str | None = "C2", **changes: object) -> Item:
    values: dict[str, object] = {
        "position": position,
        "sheet": "Security",
        "cell": cell,
        "review": "approved",
        "wording": "Yes, for all staff.",
    }
    return Item(**(values | changes))  # type: ignore[arg-type]


def skipped(plan_item: Item, sheet=None) -> Skip:  # type: ignore[no-untyped-def]
    plan = plan_writeback([plan_item], {"Security": sheet or index()})
    assert plan.writes == []
    [skip] = plan.skips
    return skip


def test_an_approved_and_an_edited_answer_are_written_to_their_cells() -> None:
    items = [item(1, "C2"), item(2, "C3", review="edited", wording="Yes. Yearly.")]

    plan = plan_writeback(items, {"Security": index()})

    assert plan.skips == []
    assert plan.writes == [
        Write(position=1, sheet="Security", cell="C2", wording="Yes, for all staff."),
        Write(position=2, sheet="Security", cell="C3", wording="Yes. Yearly."),
    ]


def test_a_rejected_or_expired_question_is_left_empty_with_that_reason() -> None:
    assert skipped(item(review="rejected", wording=None)).reason == "rejected"
    assert skipped(item(review="expired", wording=None)).reason == "expired"
    assert skipped(item(review="unanswered", wording=None)).reason == "unanswered"


def test_a_question_nobody_decided_is_never_written() -> None:
    assert skipped(item(review="pending")).reason == "undecided"
    assert skipped(item(review=None)).reason == "undecided"


def test_a_question_with_no_answer_column_is_listed() -> None:
    assert skipped(item(cell=None)).reason == "no_answer_cell"


def test_a_sheet_that_is_not_in_the_file_is_listed() -> None:
    plan = plan_writeback([item()], {})
    assert [skip.reason for skip in plan.skips] == ["sheet_missing"]


def test_a_cell_that_already_has_content_is_left_alone() -> None:
    occupied = index(b'<row r="2"><c r="C2" t="s"><v>4</v></c></row>')

    assert skipped(item(), occupied).reason == "cell_not_empty"


def test_an_existing_empty_cell_is_written() -> None:
    styled = index(b'<row r="2"><c r="C2" s="5"/></row>')

    plan = plan_writeback([item()], {"Security": styled})

    assert [write.cell for write in plan.writes] == ["C2"]


def test_wording_goes_to_the_first_cell_of_a_merged_range() -> None:
    merged = index(after=b'<mergeCells><mergeCell ref="C2:D3"/></mergeCells>')

    plan = plan_writeback([item(cell="D3")], {"Security": merged})

    assert [write.cell for write in plan.writes] == ["C2"]


def test_a_merged_range_whose_first_cell_is_taken_is_left_alone() -> None:
    merged = index(
        b'<row r="2"><c r="C2"><v>1</v></c></row>',
        b'<mergeCells><mergeCell ref="C2:D3"/></mergeCells>',
    )

    assert skipped(item(cell="D3"), merged).reason == "cell_not_empty"


def test_a_dropdown_cell_takes_only_one_of_its_values() -> None:
    choices = (
        b'<dataValidations><dataValidation type="list" sqref="C2:C9">'
        b'<formula1>"Yes,No,N/A"</formula1></dataValidation></dataValidations>'
    )
    sheet = index(after=choices)

    plan = plan_writeback([item(wording="yes")], {"Security": sheet})
    assert [write.wording for write in plan.writes] == ["yes"]

    skip = skipped(item(wording="Yes, for all staff."), sheet)
    assert (skip.reason, skip.detail) == ("dropdown", "Yes, No, N/A")


def test_a_dropdown_whose_values_cannot_be_read_is_left_alone() -> None:
    choices = (
        b'<dataValidations><dataValidation type="list" sqref="C2">'
        b"<formula1>$Z$1:$Z$3</formula1></dataValidation></dataValidations>"
    )

    skip = skipped(item(wording="Yes"), index(after=choices))

    assert (skip.reason, skip.detail) == ("dropdown", "")


def test_wording_a_cell_cannot_hold_is_listed_not_cut() -> None:
    assert skipped(item(wording="x" * 32_768)).reason == "too_long"
    assert skipped(item(wording="bell\x07")).reason == "bad_character"
    # A spreadsheet would read this as a carriage return, not as the seven characters typed.
    assert skipped(item(wording="see _x000D_ here")).reason == "bad_character"
    assert skipped(item(wording="  ")).reason == "no_answer"


def test_line_breaks_and_tabs_are_fine() -> None:
    plan = plan_writeback([item(wording="One.\nTwo.\tThree.")], {"Security": index()})
    assert len(plan.writes) == 1


def test_two_questions_aimed_at_one_cell_write_the_first_only() -> None:
    plan = plan_writeback([item(1), item(2)], {"Security": index()})

    assert [write.position for write in plan.writes] == [1]
    assert [(skip.position, skip.reason) for skip in plan.skips] == [(2, "cell_not_empty")]


# --- A dropdown with a comments column beside it. ---

from standingproof.writeback.plan import leading_choice  # noqa: E402

YES_NO = (
    b'<dataValidations><dataValidation type="list" sqref="C2:C9">'
    b'<formula1>"Yes,No,N/A"</formula1></dataValidation></dataValidations>'
)
SENTENCE = "Yes. Keys are rotated every 12 months."


def test_the_dropdowns_value_is_the_answers_first_word_only_when_it_plainly_is() -> None:
    choices = ["Yes", "No", "N/A"]
    assert leading_choice("Yes. Keys are rotated.", choices) == "Yes"
    assert leading_choice("yes, for all staff", choices) == "Yes"
    assert leading_choice("  No; not yet.", choices) == "No"
    assert leading_choice("N/A: we hold no card data", choices) == "N/A"
    assert leading_choice("Yes - with exceptions", choices) == "Yes"
    assert leading_choice("No", choices) == "No"
    # Not a value followed by a break: nothing is taken.
    assert leading_choice("Yesterday we rotated keys.", choices) is None
    assert leading_choice("Yes for employees", choices) is None
    assert leading_choice("Not applicable.", choices) is None
    assert leading_choice("We do. Yes.", choices) is None
    assert leading_choice("Yes.", []) is None


def test_the_longer_of_two_values_that_both_fit_is_taken() -> None:
    assert (
        leading_choice("No - partially. See notes.", ["No", "No - partially"]) == "No - partially"
    )


def with_comment(**changes: object) -> Item:
    return item(**({"comment_cell": "D2", "wording": SENTENCE} | changes))


def test_a_sentence_goes_to_the_comments_cell_and_its_first_word_to_the_dropdown() -> None:
    plan = plan_writeback([with_comment()], {"Security": index(after=YES_NO)})

    assert plan.skips == []
    assert [(w.cell, w.kind, w.wording) for w in plan.writes] == [
        ("C2", "choice", "Yes"),
        ("D2", "comment", SENTENCE),
    ]


def test_without_a_plain_first_word_only_the_comment_is_written() -> None:
    wording = "We enforce it for staff."
    plan = plan_writeback([with_comment(wording=wording)], {"Security": index(after=YES_NO)})

    assert [(w.cell, w.kind) for w in plan.writes] == [("D2", "comment")]
    assert [(s.cell, s.reason, s.detail) for s in plan.skips] == [
        ("C2", "dropdown", "Yes, No, N/A")
    ]


def test_an_occupied_comments_cell_means_neither_cell_is_written() -> None:
    sheet = index(b'<row r="2"><c r="D2" t="s"><v>1</v></c></row>', YES_NO)

    plan = plan_writeback([with_comment()], {"Security": sheet})

    assert plan.writes == []
    assert [(s.cell, s.reason) for s in plan.skips] == [
        ("C2", "dropdown"),
        ("D2", "cell_not_empty"),
    ]


def test_an_exact_dropdown_value_is_the_answer_and_the_comment_is_not_touched() -> None:
    plan = plan_writeback([with_comment(wording="Yes")], {"Security": index(after=YES_NO)})

    assert [(w.cell, w.kind) for w in plan.writes] == [("C2", "answer")]


def test_a_comments_cell_is_ignored_when_the_answer_cell_is_not_a_dropdown() -> None:
    plan = plan_writeback([with_comment()], {"Security": index()})

    assert [(w.cell, w.kind) for w in plan.writes] == [("C2", "answer")]


def test_a_dropdown_without_a_comments_cell_is_listed_as_before() -> None:
    plan = plan_writeback([item(wording=SENTENCE)], {"Security": index(after=YES_NO)})

    assert plan.writes == []
    assert [(s.cell, s.reason) for s in plan.skips] == [("C2", "dropdown")]


def test_a_comment_too_long_for_a_cell_writes_nothing() -> None:
    long = "Yes. " + "x" * 32_768
    plan = plan_writeback([with_comment(wording=long)], {"Security": index(after=YES_NO)})

    assert plan.writes == []
    assert {s.reason for s in plan.skips} == {"dropdown", "too_long"}
