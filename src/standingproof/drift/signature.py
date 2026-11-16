"""Was this delivery made by someone who holds our webhook secret?

GitHub signs the bytes of the request body with HMAC-SHA256 and sends the result as
`X-Hub-Signature-256: sha256=<64 hex digits>`. Pure: bytes and text in, a boolean out.
"""

import hashlib
import hmac

_PREFIX = "sha256="
_HEX_DIGITS = 64


def verify(secret: str, body: bytes, header: str | None) -> bool:
    """True only when `header` is the signature of exactly these bytes under `secret`.

    `body` must be the bytes as they arrived. Parsing and writing JSON again changes them.
    """
    if not secret or header is None or not header.startswith(_PREFIX):
        return False
    given = header[len(_PREFIX) :]
    # compare_digest raises on text that is not ASCII, and leaks nothing by length here:
    # a signature of the wrong length is refused before any comparison.
    if len(given) != _HEX_DIGITS or not given.isascii():
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Constant time: `==` stops at the first differing character, and how long it took
    # tells a caller how many leading characters were right.
    return hmac.compare_digest(expected, given)
