"""Test doubles shared across test modules."""

import hashlib
import math
import re
from collections.abc import Sequence

from standingproof.library.embedding import DEFAULT_MODEL, MODELS, ModelSpec

# Words that mean the same thing share an axis, so the fake has a small, known notion of
# "meaning": texts about the same concept point the same way without sharing a word.
_CONCEPTS: list[set[str]] = [
    {"mfa", "2fa", "multi-factor", "multifactor", "two-factor", "second", "factor"},
    {"employees", "employee", "staff", "workforce", "personnel"},
    {"encryption", "encrypted", "encrypt", "aes", "aes-256", "cipher"},
    {"backup", "backups", "restore", "recovery", "disaster", "continuity"},
    {"laptop", "laptops", "endpoint", "endpoints", "device", "devices"},
]
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")
_CONCEPT_WEIGHT = 3.0


class FakeEmbedder:
    """Deterministic vectors with no model file: same text, same vector, every run."""

    def __init__(self, spec: ModelSpec | None = None) -> None:
        self.spec = spec or MODELS[DEFAULT_MODEL]
        self.document_batches: list[list[str]] = []
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.spec.dimensions
        for word in _WORD.findall(text.lower()):
            for axis, concept in enumerate(_CONCEPTS):
                if word in concept:
                    vector[axis] += _CONCEPT_WEIGHT
            digest = hashlib.sha256(word.encode()).digest()
            bucket = len(_CONCEPTS) + int.from_bytes(digest[:4], "big") % (
                self.spec.dimensions - len(_CONCEPTS)
            )
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            vector[-1] = 1.0  # a zero vector has no direction, and cosine distance needs one
            return vector
        return [v / norm for v in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.document_batches.append(list(texts))
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)

    async def aclose(self) -> None:
        return None
