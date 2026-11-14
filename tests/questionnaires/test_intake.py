import pytest

from standingproof.questionnaires.intake import FileLimitError, UnsupportedFileError, check_xlsx
from tests.xlsx_builder import rewritten, simple, xlsx, zip_with

LIMITS = {"max_uncompressed_bytes": 50_000_000, "max_sheets": 20}


def test_accepts_a_real_workbook() -> None:
    check_xlsx(simple(["Is MFA enforced?"]), **LIMITS)


def test_rejects_text_renamed_to_xlsx() -> None:
    with pytest.raises(UnsupportedFileError):
        check_xlsx(b"ID,Question\n1,Is MFA enforced?\n", **LIMITS)


def test_rejects_a_zip_that_is_not_a_workbook() -> None:
    with pytest.raises(UnsupportedFileError):
        check_xlsx(zip_with({"notes.txt": b"hello"}), **LIMITS)


def test_rejects_a_truncated_archive() -> None:
    with pytest.raises(UnsupportedFileError):
        check_xlsx(simple(["Is MFA enforced?"])[:200], **LIMITS)


def test_rejects_a_macro_workbook_by_its_content_not_its_name() -> None:
    plain = simple(["Is MFA enforced?"])
    macro = rewritten(plain, {"xl/vbaProject.bin": b"\x00macro"})

    with pytest.raises(UnsupportedFileError, match="macro"):
        check_xlsx(macro, **LIMITS)


def test_rejects_an_archive_that_expands_past_the_limit() -> None:
    bomb = rewritten(simple(["Is MFA enforced?"]), {"xl/media/pad.bin": b"\x00" * 200_000})
    assert len(bomb) < 50_000  # small on the wire, large when expanded

    with pytest.raises(FileLimitError, match="uncompressed"):
        check_xlsx(bomb, max_uncompressed_bytes=100_000, max_sheets=20)


def test_rejects_more_sheets_than_allowed() -> None:
    many = xlsx({f"S{i}": [["Question"], ["Is MFA enforced?"]] for i in range(4)})

    with pytest.raises(FileLimitError, match="sheets"):
        check_xlsx(many, max_uncompressed_bytes=50_000_000, max_sheets=3)
