"""Turn text into vectors.

Everything else in the library depends on the `Embedder` protocol, never on a particular
model. A model is chosen by name in settings; hosted models also need their provider's key.
"""

import asyncio
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

import httpx2

from standingproof.config import Settings

if TYPE_CHECKING:
    from fastembed import TextEmbedding

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    provider: Literal["local", "voyage"]
    # How many numbers the model returns per text. The vector index is built for this width.
    dimensions: int
    # Names this model's indexes: ix_<table>_<suffix>. Must match the migration that made them.
    index_suffix: str
    # A match further away than this (cosine distance) is not a match. Without a cut-off,
    # nearest-neighbour search always returns something, however unrelated.
    max_distance: float | None


# Adding a model: add it here and add a migration that creates its two indexes.
MODELS: dict[str, ModelSpec] = {
    DEFAULT_MODEL: ModelSpec(
        name=DEFAULT_MODEL,
        provider="local",
        dimensions=384,
        index_suffix="bge_small",
        max_distance=None,
    ),
    "voyage-4-lite": ModelSpec(
        name="voyage-4-lite",
        provider="voyage",
        dimensions=512,
        index_suffix="voyage_4_lite",
        max_distance=None,
    ),
}


class EmbeddingError(Exception):
    """Texts could not be embedded: the provider failed or returned something unusable."""


class EmbeddingConfigError(Exception):
    """The embedding settings name an unknown model or lack a required key."""


class Embedder(Protocol):
    spec: ModelSpec

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """One vector per text, in the same order. For text that will be stored."""
        ...

    async def embed_query(self, text: str) -> list[float]:
        """The vector for a search query. Some models embed queries differently."""
        ...

    async def aclose(self) -> None: ...


def _checked(vectors: list[list[float]], spec: ModelSpec, expected: int) -> list[list[float]]:
    if len(vectors) != expected:
        raise EmbeddingError(f"{spec.name} returned {len(vectors)} vectors for {expected} texts.")
    for vector in vectors:
        if len(vector) != spec.dimensions:
            raise EmbeddingError(
                f"{spec.name} returned a vector of width {len(vector)}; "
                f"{spec.dimensions} was expected."
            )
    return vectors


class FastEmbedEmbedder:
    """A model that runs inside this process, on the CPU. No text leaves the machine."""

    def __init__(self, spec: ModelSpec, *, cache_dir: str | None = None) -> None:
        self.spec = spec
        self._cache_dir = cache_dir
        self._model: TextEmbedding | None = None
        # Loading takes seconds and must happen once, even if two requests arrive together.
        self._load_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _get_model(self) -> "TextEmbedding":
        with self._load_lock:
            if self._model is None:
                # Imported here: importing the runtime is slow, and code paths that never
                # embed (health checks, questionnaire uploads) should not pay for it.
                from fastembed import TextEmbedding

                self._model = TextEmbedding(self.spec.name, cache_dir=self._cache_dir)
            return self._model

    def _documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._get_model().passage_embed(texts)]

    def _query(self, text: str) -> list[float]:
        vectors: list[list[float]] = [
            vector.tolist() for vector in self._get_model().query_embed(text)
        ]
        return vectors[0]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        # Running the model is CPU work. In a thread, the event loop keeps serving requests.
        vectors = await asyncio.to_thread(self._documents, list(texts))
        return _checked(vectors, self.spec, len(texts))

    async def embed_query(self, text: str) -> list[float]:
        vector = await asyncio.to_thread(self._query, text)
        return _checked([vector], self.spec, 1)[0]

    async def aclose(self) -> None:
        return None


class VoyageEmbedder:
    """Voyage AI's hosted models, called over HTTPS. Text is sent to Voyage."""

    def __init__(
        self,
        spec: ModelSpec,
        *,
        api_key: str,
        client: httpx2.AsyncClient | None = None,
        batch_size: int = 100,
    ) -> None:
        self.spec = spec
        self._api_key = api_key
        self._client = client or httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, connect=5.0))
        self._batch_size = batch_size

    def __repr__(self) -> str:
        # The default repr would be safe too; this one says so on purpose.
        return f"VoyageEmbedder(model={self.spec.name!r}, api_key=<hidden>)"

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        try:
            response = await self._client.post(
                _VOYAGE_URL,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "input": texts,
                    "model": self.spec.name,
                    "input_type": input_type,
                    "output_dimension": self.spec.dimensions,
                },
            )
        except httpx2.HTTPError as error:
            # `from None`: the original exception holds the request, and with it the key.
            raise EmbeddingError(f"Could not reach Voyage: {type(error).__name__}.") from None
        if response.status_code != httpx2.codes.OK:
            raise EmbeddingError(f"Voyage answered {response.status_code}.")
        try:
            rows: list[dict[str, Any]] = response.json()["data"]
            vectors = [row["embedding"] for row in sorted(rows, key=lambda row: row["index"])]
        except (ValueError, KeyError, TypeError) as error:
            raise EmbeddingError("Voyage returned a reply in an unexpected shape.") from error
        return _checked(vectors, self.spec, len(texts))

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            vectors.extend(await self._embed(batch, "document"))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "query"))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


def model_spec(name: str) -> ModelSpec:
    spec = MODELS.get(name)
    if spec is None:
        raise EmbeddingConfigError(
            f"Unknown embedding model {name!r}. Known models: {', '.join(sorted(MODELS))}."
        )
    return spec


def build_embedder(settings: Settings) -> Embedder:
    """The embedder the settings ask for. Fails at startup, not on the first request."""
    spec = model_spec(settings.embedding_model)
    if spec.provider == "voyage":
        if settings.voyage_api_key is None:
            raise EmbeddingConfigError(
                f"The embedding model {spec.name!r} needs a key: set VOYAGE_API_KEY."
            )
        return VoyageEmbedder(spec, api_key=settings.voyage_api_key.get_secret_value())
    return FastEmbedEmbedder(spec, cache_dir=settings.embedding_cache_dir)
