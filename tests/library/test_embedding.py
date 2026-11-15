import json
import math
import os
import subprocess
import sys
from pathlib import Path

import httpx2
import pytest

from standingproof.config import Settings
from standingproof.library.embedding import (
    DEFAULT_MODEL,
    MODELS,
    Embedder,
    EmbeddingConfigError,
    EmbeddingError,
    FastEmbedEmbedder,
    VoyageEmbedder,
    build_embedder,
)
from tests.fakes import FakeEmbedder

VOYAGE = MODELS["voyage-4-lite"]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_the_registry_knows_each_model_and_its_width() -> None:
    assert MODELS[DEFAULT_MODEL].provider == "local"
    assert MODELS[DEFAULT_MODEL].dimensions == 384
    assert VOYAGE.provider == "voyage"
    assert VOYAGE.dimensions == 512
    # Index names are built from these, so they must be plain identifiers and distinct.
    suffixes = [spec.index_suffix for spec in MODELS.values()]
    assert all(s.replace("_", "").isalnum() for s in suffixes)
    assert len(set(suffixes)) == len(suffixes)


async def test_the_fake_embedder_has_the_embedder_shape() -> None:
    embedder: Embedder = FakeEmbedder()

    vectors = await embedder.embed_documents(["one text", "another text"])
    query = await embedder.embed_query("one text")

    assert len(vectors) == 2
    assert all(len(v) == embedder.spec.dimensions for v in vectors)
    assert query == vectors[0]


async def test_the_fake_embedder_places_same_meaning_closer_than_unrelated() -> None:
    embedder = FakeEmbedder()
    mfa, two_fa, backup = await embedder.embed_documents(
        ["multi-factor authentication for employees", "2FA for staff", "nightly backup restore"]
    )

    assert cosine(mfa, two_fa) > 0.8
    assert cosine(mfa, backup) < 0.2


def voyage_with(handler: httpx2.MockTransport, *, batch_size: int = 100) -> VoyageEmbedder:
    return VoyageEmbedder(
        VOYAGE,
        api_key="vk-test-secret",
        client=httpx2.AsyncClient(transport=handler),
        batch_size=batch_size,
    )


def embeddings_response(request: httpx2.Request, *, width: int = 512) -> httpx2.Response:
    texts = json.loads(request.content)["input"]
    data = [
        {"object": "embedding", "index": i, "embedding": [float(len(t))] + [0.0] * (width - 1)}
        for i, t in enumerate(texts)
    ]
    # Reversed on purpose: the client must order by `index`, not by position in the reply.
    return httpx2.Response(200, json={"object": "list", "data": data[::-1], "model": VOYAGE.name})


async def test_voyage_sends_the_key_model_width_and_input_type() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return embeddings_response(request)

    embedder = voyage_with(httpx2.MockTransport(handler))
    await embedder.embed_documents(["a policy paragraph"])
    await embedder.embed_query("a question")

    assert str(seen[0].url) == "https://api.voyageai.com/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer vk-test-secret"
    document_body = json.loads(seen[0].content)
    assert document_body == {
        "input": ["a policy paragraph"],
        "model": "voyage-4-lite",
        "input_type": "document",
        "output_dimension": 512,
    }
    assert json.loads(seen[1].content)["input_type"] == "query"


async def test_voyage_returns_vectors_in_the_order_the_texts_were_given() -> None:
    embedder = voyage_with(httpx2.MockTransport(embeddings_response))

    vectors = await embedder.embed_documents(["a", "bbb", "cc"])

    assert [v[0] for v in vectors] == [1.0, 3.0, 2.0]


async def test_voyage_splits_a_large_input_into_batches() -> None:
    sizes: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sizes.append(len(json.loads(request.content)["input"]))
        return embeddings_response(request)

    embedder = voyage_with(httpx2.MockTransport(handler), batch_size=2)
    vectors = await embedder.embed_documents(["a", "bb", "ccc", "dddd", "eeeee"])

    assert sizes == [2, 2, 1]
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]


async def test_voyage_embeds_nothing_without_a_request() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("no request expected")

    assert await voyage_with(httpx2.MockTransport(handler)).embed_documents([]) == []


async def test_a_refused_voyage_request_is_an_error_that_does_not_show_the_key() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"detail": "Provided API key is invalid."})

    with pytest.raises(EmbeddingError) as raised:
        await voyage_with(httpx2.MockTransport(handler)).embed_query("q")

    assert "401" in str(raised.value)
    assert "vk-test-secret" not in str(raised.value)
    assert "vk-test-secret" not in repr(raised.value.__cause__)


async def test_a_network_failure_is_an_embedding_error() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(EmbeddingError):
        await voyage_with(httpx2.MockTransport(handler)).embed_query("q")


async def test_a_vector_of_the_wrong_width_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return embeddings_response(request, width=1024)

    with pytest.raises(EmbeddingError, match="512"):
        await voyage_with(httpx2.MockTransport(handler)).embed_query("q")


async def test_a_reply_with_too_few_vectors_is_refused() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"object": "list", "data": []})

    with pytest.raises(EmbeddingError):
        await voyage_with(httpx2.MockTransport(handler)).embed_documents(["a"])


def test_the_default_settings_build_the_local_embedder_without_loading_the_model() -> None:
    embedder = build_embedder(Settings(_env_file=None))

    assert isinstance(embedder, FastEmbedEmbedder)
    assert embedder.spec is MODELS[DEFAULT_MODEL]
    assert embedder.loaded is False


def test_a_voyage_model_with_a_key_builds_the_voyage_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test-secret")

    embedder = build_embedder(Settings(_env_file=None, embedding_model="voyage-4-lite"))

    assert isinstance(embedder, VoyageEmbedder)
    assert "vk-test-secret" not in repr(embedder)


def test_a_voyage_model_without_a_key_is_refused_with_the_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    with pytest.raises(EmbeddingConfigError, match="VOYAGE_API_KEY"):
        build_embedder(Settings(_env_file=None, embedding_model="voyage-4-lite"))


def test_an_unknown_model_is_refused_and_the_known_ones_are_listed() -> None:
    with pytest.raises(EmbeddingConfigError, match="voyage-4-lite"):
        build_embedder(Settings(_env_file=None, embedding_model="made-up-model"))


@pytest.mark.slow
async def test_the_real_local_model_gives_384_numbers_and_knows_a_paraphrase() -> None:
    embedder = build_embedder(Settings(_env_file=None))

    mfa, encryption = await embedder.embed_documents(
        [
            "Multi-factor authentication is required for all employee accounts.",
            "Customer data is encrypted at rest using AES-256.",
        ]
    )
    question = await embedder.embed_query("Do you enforce 2FA for staff?")

    assert len(mfa) == 384
    assert cosine(question, mfa) > cosine(question, encryption)


async def test_the_local_model_runs_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import time

    embedder = FastEmbedEmbedder(MODELS[DEFAULT_MODEL])

    def slow_model(texts: list[str]) -> list[list[float]]:
        time.sleep(0.3)  # stands in for the model; blocks whichever thread runs it
        return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(embedder, "_documents", slow_model)
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await embedder.embed_documents(["text"])
    finally:
        task.cancel()

    # If the model ran on the event loop, the ticker could not have run at all.
    assert ticks >= 10


def test_an_empty_voyage_key_counts_as_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Compose passes the variable through as an empty string when it is not set.
    monkeypatch.setenv("VOYAGE_API_KEY", "")

    with pytest.raises(EmbeddingConfigError, match="VOYAGE_API_KEY"):
        build_embedder(Settings(_env_file=None, embedding_model="voyage-4-lite"))


@pytest.mark.slow
def test_the_model_runtime_records_no_telemetry(tmp_path: Path) -> None:
    # The ONNX runtime ships with usage telemetry on: each process queues an event (machine
    # details, the interpreter's path) in a file under the home directory and uploads the
    # queue to its vendor. A fresh home directory shows whether anything was queued.
    script = (
        "import asyncio\n"
        "from standingproof.config import Settings\n"
        "from standingproof.library.embedding import build_embedder\n"
        "embedder = build_embedder(Settings(_env_file=None))\n"
        "print(len(asyncio.run(embedder.embed_query('a question'))))\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"ORT_DISABLE_TELEMETRY"}}
    env.update(HOME=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / ".cache"))

    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=120
    )

    assert done.stdout.strip() == "384", done.stderr
    written = [str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()]
    assert [p for p in written if "onnxruntime" in p.lower() or "Microsoft" in p] == []
