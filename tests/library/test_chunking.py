import pytest

from standingproof.library.chunking import TooManyChunksError, chunk_markdown

POLICY = """# Access Control Policy

Intro paragraph about scope.

## 4.2 Authentication

All workforce members must authenticate with a second factor.

SMS is not an accepted factor.

## 4.3 Reviews

Access is reviewed every quarter.
"""


def chunks(text: str, *, max_chars: int = 1200, max_chunks: int = 500):  # type: ignore[no-untyped-def]
    return chunk_markdown(text, max_chars=max_chars, max_chunks=max_chunks)


def test_each_heading_starts_a_chunk_with_its_heading_path() -> None:
    result = chunks(POLICY)

    assert [c.heading_path for c in result] == [
        "Access Control Policy",
        "Access Control Policy > 4.2 Authentication",
        "Access Control Policy > 4.3 Reviews",
    ]
    assert result[1].text == (
        "All workforce members must authenticate with a second factor.\n\n"
        "SMS is not an accepted factor."
    )
    assert [c.position for c in result] == [1, 2, 3]


def test_a_sibling_heading_replaces_the_previous_one_in_the_path() -> None:
    text = "# A\n\n## B\n\nb text\n\n### C\n\nc text\n\n## D\n\nd text\n"

    assert [c.heading_path for c in chunks(text)] == ["A > B", "A > B > C", "A > D"]


def test_a_long_section_is_split_at_paragraphs_and_stays_under_the_limit() -> None:
    paragraphs = [f"Paragraph {n} " + "word " * 30 for n in range(10)]
    text = "# Long\n\n" + "\n\n".join(paragraphs)

    result = chunks(text, max_chars=400)

    assert len(result) > 1
    assert all(len(c.text) <= 400 for c in result)
    assert all(c.heading_path == "Long" for c in result)
    # Nothing lost and nothing repeated: every paragraph appears exactly once.
    joined = "\n\n".join(c.text for c in result)
    assert all(joined.count(p.strip()) == 1 for p in paragraphs)


def test_a_paragraph_longer_than_the_limit_is_split_at_sentences() -> None:
    sentences = [f"Sentence number {n} is here." for n in range(40)]
    text = "# S\n\n" + " ".join(sentences)

    result = chunks(text, max_chars=200)

    assert all(len(c.text) <= 200 for c in result)
    assert all(c.text.endswith(".") for c in result)


def test_one_unbroken_run_longer_than_the_limit_is_cut_hard() -> None:
    result = chunks("x" * 950, max_chars=300)

    assert [len(c.text) for c in result] == [300, 300, 300, 50]


def test_text_without_headings_still_chunks_with_an_empty_path() -> None:
    result = chunks("Just a paragraph.\n\nAnd another.")

    assert len(result) == 1
    assert result[0].heading_path == ""


def test_a_hash_inside_a_code_fence_is_not_a_heading() -> None:
    text = "# Real\n\n```\n# not a heading\n```\n"

    result = chunks(text)

    assert [c.heading_path for c in result] == ["Real"]
    assert "# not a heading" in result[0].text


def test_empty_input_and_headings_without_text_give_no_chunks() -> None:
    assert chunks("") == []
    assert chunks("   \n\n") == []
    assert chunks("# Only a heading\n\n## And another\n") == []


def test_too_many_chunks_is_an_error_that_names_the_limit() -> None:
    text = "\n\n".join(f"# H{n}\n\nbody {n}" for n in range(6))

    with pytest.raises(TooManyChunksError, match="5"):
        chunks(text, max_chunks=5)
