"""Checks on an uploaded file's raw bytes, made before any parser opens it."""

import zipfile
from io import BytesIO

_ZIP_SIGNATURE = b"PK\x03\x04"
_REQUIRED_ENTRIES = ("[Content_Types].xml", "xl/workbook.xml")
_MACRO_CONTENT_TYPE = b"macroEnabled"
_MAX_CONTENT_TYPES_BYTES = 1_000_000


class UnsupportedFileError(Exception):
    """The upload is not an .xlsx workbook we accept."""


class FileLimitError(Exception):
    """The upload is a workbook, but larger than a configured limit."""


def check_xlsx(content: bytes, *, max_uncompressed_bytes: int, max_sheets: int) -> None:
    """Raise unless `content` is a plain .xlsx workbook inside the limits.

    An .xlsx file is a zip archive of XML files. Everything here reads the archive's
    directory only, so a hostile file is rejected before any of it is expanded or parsed.
    The file's name and declared media type are ignored: both are chosen by the sender.
    """
    if not content.startswith(_ZIP_SIGNATURE):
        raise UnsupportedFileError("the file is not an .xlsx workbook")
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as error:
        raise UnsupportedFileError("the file is not a readable .xlsx workbook") from error

    with archive:
        entries = archive.infolist()
        names = {entry.filename for entry in entries}
        if not all(required in names for required in _REQUIRED_ENTRIES):
            raise UnsupportedFileError("the file is a zip archive but not an .xlsx workbook")

        # Sizes come from the archive directory. Python's zipfile never reads more than the
        # declared size of an entry, so a directory that understates sizes cannot be used to
        # expand more than this total.
        total = sum(entry.file_size for entry in entries)
        if total > max_uncompressed_bytes:
            raise FileLimitError(
                f"the workbook is {total} bytes uncompressed; the limit is {max_uncompressed_bytes}"
            )

        content_types = archive.getinfo("[Content_Types].xml")
        if content_types.file_size > _MAX_CONTENT_TYPES_BYTES:
            raise UnsupportedFileError("the workbook's content type list is not valid")
        if "xl/vbaProject.bin" in names or _MACRO_CONTENT_TYPE in archive.read(content_types):
            raise UnsupportedFileError(
                "macro-enabled workbooks are not accepted; save the file as .xlsx"
            )

        sheets = sum(
            1
            for name in names
            if name.startswith(("xl/worksheets/", "xl/chartsheets/")) and name.endswith(".xml")
        )
        if sheets > max_sheets:
            raise FileLimitError(f"the workbook has {sheets} sheets; the limit is {max_sheets}")
