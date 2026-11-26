# app/agentic/kb/ingest.py
"""Publish validated articles into agent_kb_articles / agent_kb_chunks.

Runs inside the caller's transaction (scripts/kb_ingest.py commits once, as
ric_kb_admin). Per article:
    unchanged   same content hash as the published version -> nothing, unless the
                chunks are not embedded with the configured model (or --reembed):
                then only its chunks are rebuilt
    changed     new version: the old one is retired (kept: guides pinned to it keep
                working), its chunks deleted, the new version and chunks inserted
    added       version 1
    retired     published, but its file is gone
Embeddings of unchanged chunk text are reused (same text hash and model) unless
reembed. Vectors are written as text and cast in SQL, so no driver codec is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.kb.articles import Article, ChunkSpec, build_chunks
from app.agentic.kb.embeddings import Embedder, vector_literal
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.values import KbArticleStatus
from app.models import AgentKbArticle, AgentKbChunk

INSERT_CHUNK = text(
    "INSERT INTO agent_kb_chunks (id, article_id, chunk_type, step_id, chunk_text, text_hash, "
    "embedding, embedding_model) VALUES (:id, :article_id, :chunk_type, :step_id, :chunk_text, "
    ":text_hash, CAST(CAST(:embedding AS text) AS vector), :embedding_model)"
)
CACHED_VECTORS = text(
    "SELECT DISTINCT ON (text_hash) text_hash, CAST(embedding AS text) AS vector "
    "FROM agent_kb_chunks WHERE embedding_model = :model "
    "AND text_hash = ANY(CAST(:hashes AS text[]))"
)
STALE_CHUNKS = text(
    "SELECT count(*) FROM agent_kb_chunks WHERE article_id = :article_id "
    "AND embedding_model IS DISTINCT FROM CAST(:model AS text)"
)


@dataclass
class PublishReport:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    reembedded: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    chunks_written: int = 0
    vectors_computed: int = 0
    vectors_reused: int = 0
    embedding_model: str | None = None


async def _vectors(
    session: AsyncSession,
    chunks: Sequence[ChunkSpec],
    embedder: Embedder | None,
    reembed: bool,
    report: PublishReport,
) -> list[str | None]:
    if embedder is None:
        return [None] * len(chunks)
    cached: dict[str, str] = {}
    if not reembed:
        rows = await session.execute(
            CACHED_VECTORS,
            {"model": embedder.model_key, "hashes": [c.text_hash for c in chunks]},
        )
        cached = {row.text_hash: row.vector for row in rows}
    missing = [c for c in chunks if c.text_hash not in cached]
    if missing:
        vectors = await embedder.embed([c.text for c in missing], "passage")
        for chunk, vector in zip(missing, vectors, strict=True):
            cached[chunk.text_hash] = vector_literal(vector)
    report.vectors_computed += len(missing)
    report.vectors_reused += len(chunks) - len(missing)
    return [cached[c.text_hash] for c in chunks]


async def _write_chunks(
    session: AsyncSession,
    article_id: uuid.UUID,
    chunks: Sequence[ChunkSpec],
    vectors: Sequence[str | None],
    model_key: str | None,
) -> None:
    for chunk, vector in zip(chunks, vectors, strict=True):
        await session.execute(
            INSERT_CHUNK,
            {
                "id": uuid.uuid4(),
                "article_id": article_id,
                "chunk_type": chunk.chunk_type,
                "step_id": chunk.step_id,
                "chunk_text": chunk.text,
                "text_hash": chunk.text_hash,
                "embedding": vector,
                "embedding_model": model_key if vector is not None else None,
            },
        )


async def _retire(session: AsyncSession, article_id: uuid.UUID, now: datetime) -> None:
    await session.execute(
        update(AgentKbArticle)
        .where(AgentKbArticle.id == article_id)
        .values(status=KbArticleStatus.RETIRED, retired_at=now)
    )
    await session.execute(delete(AgentKbChunk).where(AgentKbChunk.article_id == article_id))


async def publish_articles(
    session: AsyncSession,
    articles: Sequence[Article],
    catalogue: Catalogue,
    *,
    embedder: Embedder | None,
    now: datetime,
    reembed: bool = False,
) -> PublishReport:
    report = PublishReport(embedding_model=embedder.model_key if embedder else None)
    model_key = report.embedding_model
    published = {
        row.kb_ref: row
        for row in await session.execute(
            select(AgentKbArticle.id, AgentKbArticle.kb_ref, AgentKbArticle.content_hash).where(
                AgentKbArticle.status == KbArticleStatus.PUBLISHED
            )
        )
    }
    versions = dict(
        (
            await session.execute(
                select(AgentKbArticle.kb_ref, func.max(AgentKbArticle.version)).group_by(
                    AgentKbArticle.kb_ref
                )
            )
        ).all()
    )

    for article in sorted(articles, key=lambda a: a.kb_ref):
        chunks = build_chunks(article, catalogue)
        content_hash = article.content_hash()
        current = published.get(article.kb_ref)
        if current is not None and current.content_hash == content_hash:
            stale = await session.scalar(
                STALE_CHUNKS, {"article_id": current.id, "model": model_key}
            )
            if not reembed and not stale:
                report.unchanged.append(article.kb_ref)
                continue
            vectors = await _vectors(session, chunks, embedder, reembed, report)
            await session.execute(delete(AgentKbChunk).where(AgentKbChunk.article_id == current.id))
            await _write_chunks(session, current.id, chunks, vectors, model_key)
            report.chunks_written += len(chunks)
            report.reembedded.append(article.kb_ref)
            continue

        vectors = await _vectors(session, chunks, embedder, reembed, report)
        if current is not None:
            await _retire(session, current.id, now)
        article_id = uuid.uuid4()
        version = int(versions.get(article.kb_ref) or 0) + 1
        await session.execute(
            insert(AgentKbArticle).values(
                id=article_id,
                kb_ref=article.kb_ref,
                version=version,
                status=KbArticleStatus.PUBLISHED,
                title=article.title,
                summary=article.summary,
                applies_to=list(article.applies_to),
                fault_codes=list(article.fault_codes),
                kb_tags=list(article.kb_tags),
                audience=article.audience,
                content=article.content(),
                content_hash=content_hash,
                published_at=now,
            )
        )
        await _write_chunks(session, article_id, chunks, vectors, model_key)
        report.chunks_written += len(chunks)
        (report.changed if current is not None else report.added).append(
            f"{article.kb_ref} v{version}"
        )

    in_files = {a.kb_ref for a in articles}
    for kb_ref, row in sorted(published.items()):
        if kb_ref not in in_files:
            await _retire(session, row.id, now)
            report.retired.append(kb_ref)
    return report
