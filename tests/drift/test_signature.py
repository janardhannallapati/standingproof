"""A delivery is accepted only when its signature was made with our secret over its bytes."""

import hashlib
import hmac

import pytest

from standingproof.drift.signature import verify

# GitHub's own example, from "Validating webhook deliveries" in its documentation.
SECRET = "It's a Secret to Everybody"  # noqa: S105 - published test value
BODY = b"Hello, World!"
HEADER = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"


def test_githubs_published_example_is_accepted() -> None:
    assert verify(SECRET, BODY, HEADER) is True


def test_one_changed_byte_of_the_body_is_refused() -> None:
    assert verify(SECRET, b"Hello, World?", HEADER) is False


def test_the_same_json_with_other_spacing_is_other_bytes() -> None:
    body = b'{"action":"edited"}'
    header = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    assert verify(SECRET, body, header) is True
    assert verify(SECRET, b'{"action": "edited"}', header) is False


def test_another_secret_is_refused() -> None:
    assert verify("another secret", BODY, HEADER) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        HEADER.removeprefix("sha256="),  # no prefix
        HEADER[:-2],  # too short
        HEADER + "00",  # too long
        "sha256=" + "z" * 64,  # not hexadecimal
        HEADER.upper(),  # the prefix is lower case
        " " + HEADER,
        # A real SHA-1 signature of the same body: the legacy header's form.
        "sha1=" + hmac.new(SECRET.encode(), BODY, hashlib.sha1).hexdigest(),
        "sha256=" + "é" * 64,  # not ASCII: must be refused, not raise
    ],
)
def test_a_missing_or_malformed_header_is_refused(header: str | None) -> None:
    assert verify(SECRET, BODY, header) is False


def test_no_secret_accepts_nothing() -> None:
    # An HMAC with an empty key is still an HMAC. Anyone can compute it.
    forged = "sha256=" + hmac.new(b"", BODY, hashlib.sha256).hexdigest()

    assert verify("", BODY, forged) is False
