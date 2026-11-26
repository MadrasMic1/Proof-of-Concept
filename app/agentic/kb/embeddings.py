# app/agentic/kb/embeddings.py
"""Embedding clients for KB search: NVIDIA NIM now, Azure AI Foundry later.

Both speak an OpenAI-style POST {base_url}/embeddings. Differences handled here:
- NVIDIA retrieval models take input_type ("query" for searches, "passage" for KB
  text) and accept a "dimensions" size; their vectors are larger than the 1536-wide
  column, so KB_EMBEDDING_REQUEST_DIM asks for a smaller size (e.g. 1024).
- Foundry (text-embedding-3-small) is 1536 wide already; no input_type.

Every vector is zero-padded to 1536 (agent_kb_chunks.embedding vector(1536)).
Zero padding changes neither dot products nor lengths, so cosine similarity is
exactly the same. Vectors of different models are never compared: each chunk stores
model_key and searches only use chunks of the configured model.

Switching provider = .env change + "scripts.kb_ingest --publish --reembed".
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Literal, Protocol

import httpx

from app.agentic.config import AgenticSettings
from app.agentic.values import EMBEDDING_DIM
from app.core.config import Settings

EmbeddingKind = Literal["query", "passage"]
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "nvidia/nemotron-3.5-embed-1b"
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})
RETRY_DELAYS_SECONDS = (2.0, 5.0)
FAKE_DIM = 64


class EmbeddingError(RuntimeError):
    """The embedding API failed or returned something unusable."""


class Embedder(Protocol):
    model_key: str

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]: ...


def pad_vector(values: Sequence[float], dim: int = EMBEDDING_DIM) -> list[float]:
    if len(values) > dim:
        raise EmbeddingError(
            f"the model returned {len(values)} dimensions; the KB column holds {dim}. "
            "Set KB_EMBEDDING_REQUEST_DIM to 1536 or less."
        )
    return [float(v) for v in values] + [0.0] * (dim - len(values))


def vector_literal(values: Sequence[float]) -> str:
    """pgvector's text form, e.g. [0.1,0.2]."""
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"


class FakeEmbedder:
    """Deterministic bag-of-words vectors for tests and offline runs (no network)."""

    model_key = f"fake:hash:{FAKE_DIM}"

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]:
        vectors = []
        for text in texts:
            raw = [0.0] * FAKE_DIM
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                if len(word) < 3:
                    continue
                index = int(hashlib.sha1(word[:5].encode()).hexdigest(), 16) % FAKE_DIM
                raw[index] += 1.0
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            vectors.append(pad_vector([v / norm for v in raw]))
        return vectors


class HttpEmbedder:
    def __init__(
        self,
        *,
        provider: Literal["nvidia", "foundry"],
        base_url: str,
        api_key: str,
        model: str,
        request_dim: int | None,
        timeout_seconds: float,
        batch_size: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.url = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self.model = model
        self.request_dim = request_dim
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self._transport = transport
        self.model_key = f"{provider}:{model}:{request_dim or 'native'}"

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}
        if self.provider == "foundry":
            headers["api-key"] = self._api_key
        return headers

    def _payload(self, texts: Sequence[str], kind: EmbeddingKind) -> dict[str, object]:
        payload: dict[str, object] = {
            "input": list(texts),
            "model": self.model,
            "encoding_format": "float",
        }
        if self.provider == "nvidia":
            payload["input_type"] = kind
        elif self.request_dim is not None:
            payload["dimensions"] = self.request_dim
        return payload

    def _shorten(self, values: list[float]) -> list[float]:
        """Matryoshka models (NVIDIA): keep the first request_dim values, re-normalised."""
        if self.request_dim is None or len(values) <= self.request_dim:
            return values
        head = values[: self.request_dim]
        norm = math.sqrt(sum(v * v for v in head)) or 1.0
        return [v / norm for v in head]

    async def _post(
        self, client: httpx.AsyncClient, payload: dict[str, object]
    ) -> list[list[float]]:
        for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
            try:
                response = await client.post(self.url, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                if attempt < len(RETRY_DELAYS_SECONDS):
                    await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                    continue
                raise EmbeddingError(f"{self.provider} embeddings unreachable: {exc}") from exc
            if response.status_code in RETRY_STATUS and attempt < len(RETRY_DELAYS_SECONDS):
                await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.status_code != 200:
                raise EmbeddingError(
                    f"{self.provider} embeddings HTTP {response.status_code}: {response.text[:300]}"
                )
            try:
                items = sorted(response.json()["data"], key=lambda item: item["index"])
                return [[float(v) for v in item["embedding"]] for item in items]
            except (ValueError, KeyError, TypeError) as exc:
                raise EmbeddingError(f"{self.provider} embeddings: unexpected answer") from exc
        raise EmbeddingError(f"{self.provider} embeddings: no answer")

    async def embed(self, texts: Sequence[str], kind: EmbeddingKind) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self._transport
        ) as client:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                raw = await self._post(client, self._payload(batch, kind))
                if len(raw) != len(batch):
                    raise EmbeddingError(
                        f"{self.provider} embeddings: {len(raw)} vectors for {len(batch)} texts"
                    )
                vectors += [pad_vector(self._shorten(v)) for v in raw]
        return vectors


def build_embedder(agentic: AgenticSettings, core: Settings) -> Embedder | None:
    """The configured embedder, or None when KB_EMBEDDING_PROVIDER=none.

    The API key falls back to LLM_API_KEY when the worker's LLM uses the same
    provider (one NVIDIA key for chat and embeddings)."""
    provider = agentic.kb_embedding_provider
    if provider == "none":
        return None
    if provider == "fake":
        return FakeEmbedder()
    key = agentic.kb_embedding_api_key
    if key is None and core.llm_provider == provider:
        key = core.llm_api_key
    if key is None:
        raise EmbeddingError(f"KB_EMBEDDING_PROVIDER={provider} needs KB_EMBEDDING_API_KEY")
    if provider == "nvidia":
        base_url = agentic.kb_embedding_base_url or NVIDIA_BASE_URL
        model = agentic.kb_embedding_model or NVIDIA_DEFAULT_MODEL
    else:
        if not agentic.kb_embedding_base_url or not agentic.kb_embedding_model:
            raise EmbeddingError(
                "KB_EMBEDDING_PROVIDER=foundry needs KB_EMBEDDING_BASE_URL "
                "(https://<resource>.openai.azure.com/openai/v1/) and KB_EMBEDDING_MODEL "
                "(the deployment name)"
            )
        base_url, model = agentic.kb_embedding_base_url, agentic.kb_embedding_model
    return HttpEmbedder(
        provider=provider,
        base_url=base_url,
        api_key=key.get_secret_value(),
        model=model,
        request_dim=agentic.kb_embedding_request_dim,
        timeout_seconds=agentic.kb_embedding_timeout_seconds,
        batch_size=agentic.kb_embedding_batch_size,
    )
