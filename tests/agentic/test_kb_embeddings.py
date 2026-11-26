# tests/agentic/test_kb_embeddings.py
"""Agentic Phase 3: embedding clients (no network: httpx.MockTransport)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.agentic.config import AgenticSettings
from app.agentic.kb import embeddings
from app.agentic.kb.embeddings import (
    EmbeddingError,
    FakeEmbedder,
    HttpEmbedder,
    build_embedder,
    pad_vector,
    vector_literal,
)
from app.agentic.kb.retrieval import rrf, tsquery_for


def _settings(**values: Any) -> AgenticSettings:
    return AgenticSettings(_env_file=None, environment="test", **values)


def _core(provider: str = "nvidia", key: str | None = "nvapi-test") -> SimpleNamespace:
    return SimpleNamespace(llm_provider=provider, llm_api_key=SecretStr(key) if key else None)


def _embedder(provider: str, handler: Any, request_dim: int | None = None) -> HttpEmbedder:
    return HttpEmbedder(
        provider=provider,
        base_url="https://example.test/v1/",
        api_key="secret-key",
        model="test-model",
        request_dim=request_dim,
        timeout_seconds=5,
        batch_size=2,
        transport=httpx.MockTransport(handler),
    )


def _answer(request: httpx.Request, size: int) -> httpx.Response:
    texts = json.loads(request.content)["input"]
    data = [{"index": i, "embedding": [0.5] * size} for i in reversed(range(len(texts)))]
    return httpx.Response(200, json={"data": data})


def test_padding_keeps_cosine_and_rejects_large_vectors() -> None:
    padded = pad_vector([0.6, 0.8])
    assert len(padded) == 1536 and padded[:2] == [0.6, 0.8] and set(padded[2:]) == {0.0}
    with pytest.raises(EmbeddingError, match="KB_EMBEDDING_REQUEST_DIM"):
        pad_vector([0.1] * 2048)
    assert vector_literal([0.25, 1.0]) == "[0.25,1]"


async def test_nvidia_request_and_batching() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://example.test/v1/embeddings"
        assert request.headers["authorization"] == "Bearer secret-key"
        assert "api-key" not in request.headers
        seen.append(json.loads(request.content))
        return _answer(request, 2048)

    vectors = await _embedder("nvidia", handler, 768).embed(["a", "b", "c"], "passage")
    assert len(vectors) == 3 and all(len(v) == 1536 for v in vectors)
    assert [len(p["input"]) for p in seen] == [2, 1]
    assert seen[0]["input_type"] == "passage" and seen[0]["model"] == "test-model"
    assert "dimensions" not in seen[0] and "truncate" not in seen[0]
    head = vectors[0][:768]
    assert abs(sum(v * v for v in head) - 1.0) < 1e-9 and set(vectors[0][768:]) == {0.0}


async def test_foundry_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert request.headers["api-key"] == "secret-key"
        assert "input_type" not in body and "dimensions" not in body
        return _answer(request, 1536)

    embedder = _embedder("foundry", handler)
    assert embedder.model_key == "foundry:test-model:native"
    [vector] = await embedder.embed(["query"], "query")
    assert len(vector) == 1536


async def test_retry_then_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embeddings, "RETRY_DELAYS_SECONDS", (0.0, 0.0))
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] == 1 else _answer(request, 8)

    assert len(await _embedder("nvidia", flaky).embed(["x"], "query")) == 1
    assert calls["n"] == 2

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="model not found")

    with pytest.raises(EmbeddingError, match="HTTP 400: model not found"):
        await _embedder("nvidia", broken).embed(["x"], "query")


def test_build_embedder() -> None:
    assert build_embedder(_settings(), _core()) is None
    fake = build_embedder(_settings(kb_embedding_provider="fake"), _core())
    assert isinstance(fake, FakeEmbedder)
    nvidia = build_embedder(
        _settings(kb_embedding_provider="nvidia", kb_embedding_request_dim=768), _core()
    )
    assert isinstance(nvidia, HttpEmbedder)
    assert nvidia.url == "https://integrate.api.nvidia.com/v1/embeddings"
    assert nvidia.model_key == "nvidia:nvidia/nemotron-3.5-embed-1b:768"
    with pytest.raises(EmbeddingError, match="KB_EMBEDDING_API_KEY"):
        build_embedder(_settings(kb_embedding_provider="nvidia"), _core("groq"))
    with pytest.raises(EmbeddingError, match="KB_EMBEDDING_BASE_URL"):
        build_embedder(
            _settings(kb_embedding_provider="foundry", kb_embedding_api_key="k"), _core()
        )
    assert _settings(kb_embedding_request_dim="").kb_embedding_request_dim is None


async def test_fake_embedder_is_deterministic() -> None:
    fake = FakeEmbedder()
    [a, b] = await fake.embed(["printer paper jam", "printer paper jam"], "passage")
    assert a == b and len(a) == 1536


def test_tsquery_and_rrf() -> None:
    assert tsquery_for("The printer is JAMMED!") == "printer | jammed"
    assert tsquery_for("is the a") is None
    first, second = uuid.uuid4(), uuid.uuid4()
    scores = rrf([[first, second], [second]], 60)
    assert scores[second] > scores[first]
