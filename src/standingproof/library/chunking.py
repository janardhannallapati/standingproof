"""Split a policy document into pieces small enough to embed and to cite.

Pure functions: text in, chunks out. No database and no model.
"""

import re
from dataclasses import dataclass

# Deliberately simple: one fixed prefix, then the rest of the line. The closing hashes of
# "## Title ##" are stripped in code. A pattern that also matched them had several parts
# competing for the same spaces, and a crafted line made it take minutes.
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")


class TooManyChunksError(Exception):
    """The document would produce more chunks than the configured limit."""


@dataclass(frozen=True)
class Chunk:
    position: int  # order within the document, starting at 1
    heading_path: str  # "Access Control > 4.2 Authentication"; empty when there is none
    text: str


def _hard_cut(text: str, max_chars: int) -> list[str]:
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def _pack(units: list[str], separator: str, max_chars: int) -> list[str]:
    """Join consecutive units into pieces of at most max_chars. Units must each fit."""
    pieces: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}{separator}{unit}" if current else unit
        if len(candidate) <= max_chars:
            current = candidate
        else:
            pieces.append(current)
            current = unit
    if current:
        pieces.append(current)
    return pieces


def _split_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Break one over-long paragraph at sentence ends, and only then mid-sentence."""
    sentences: list[str] = []
    for sentence in _SENTENCE_BREAK.split(paragraph):
        sentences.extend(
            _hard_cut(sentence, max_chars) if len(sentence) > max_chars else [sentence]
        )
    return _pack(sentences, " ", max_chars)


def _split_section(body: str, max_chars: int) -> list[str]:
    units: list[str] = []
    for paragraph in _PARAGRAPH_BREAK.split(body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            units.append(paragraph)
        else:
            units.extend(_split_paragraph(paragraph, max_chars))
    return _pack(units, "\n\n", max_chars)


def chunk_markdown(text: str, *, max_chars: int, max_chunks: int) -> list[Chunk]:
    """Split Markdown or plain text by heading, then by size.

    A chunk never crosses a heading, so it stays on one topic, and it carries the path of
    headings above it, so a citation can say where in the document it came from.
    """
    sections: list[tuple[str, str]] = []  # (heading path, body)
    path: list[tuple[int, str]] = []  # (level, title) of the headings we are under
    body: list[str] = []
    in_fence = False

    def close_section() -> None:
        sections.append((" > ".join(title for _, title in path), "\n".join(body)))
        body.clear()

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else _HEADING.match(line)
        title = heading.group(2).rstrip().rstrip("#").rstrip() if heading else ""
        if heading is None or not title:
            body.append(line)
            continue
        close_section()
        level = len(heading.group(1))
        # A heading ends every open heading at its own level or deeper.
        while path and path[-1][0] >= level:
            path.pop()
        path.append((level, title))
    close_section()

    chunks: list[Chunk] = []
    for heading_path, section_body in sections:
        for piece in _split_section(section_body, max_chars):
            if len(chunks) >= max_chunks:
                raise TooManyChunksError(
                    f"The document splits into more than {max_chunks} chunks, the limit."
                )
            chunks.append(Chunk(position=len(chunks) + 1, heading_path=heading_path, text=piece))
    return chunks
