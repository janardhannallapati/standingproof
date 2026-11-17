"""A record of who decided what, kept so that a later change to it can be seen.

Each entry carries a hash of itself and of the entry before it. Changing, removing or
reordering any entry breaks every hash after it, and `verify` names the first that fails.
See `chain.py` for what this proves and what it does not.
"""

from standingproof.audit.chain import Broken, Entry, head, record, verify

__all__ = ["Broken", "Entry", "head", "record", "verify"]
