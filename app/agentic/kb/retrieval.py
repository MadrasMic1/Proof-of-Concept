# app/agentic/kb/retrieval.py
"""Find the right published article: exact first, then tags, then search.

1. exact   element type + fault code (from a Store Insight finding)   score 1.0
2. tags    the finding's kb_tags overlap the article's tags             score 0.9
3. search  the user's words: full-text (any word) + vector similarity
           (when an embedder is configured), merged with reciprocal rank
           fusion: an article at rank r in a list adds 1 / (KB_SEARCH_RRF_K + r)
The first method that finds something wins. Every method keeps only published
store-staff articles and, when the element type is known, articles for that type.
If the embedding API fails, search continues with full-text only (warning).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.kb.embeddings import Embedder, EmbeddingError, vector_literal

MatchedBy = Literal["exact", "tag", "search"]
STOPWORDS = frozenset(
    "a an and are at be but by can for from has have i in is it its my not of on or "
    "our so that the this to was we with what when why how does do doesnt dont".split()
)

_BASE = (
    "SELECT a.id, a.kb_ref, a.version, a.title, a.summary FROM agent_kb_articles a "
    "WHERE a.status = 'published' AND a.audience = 'store_staff' "
    "AND (CAST(:element_type AS text) IS NULL OR CAST(:element_type AS text) = ANY(a.applies_to)) "
)
EXACT = text(_BASE + "AND CAST(:fault_code AS text) = ANY(a.fault_codes) ORDER BY a.kb_ref")
TAGS = text(
    _BASE + "AND a.kb_tags && CAST(:tags AS text[]) "
    "ORDER BY cardinality(ARRAY(SELECT unnest(a.kb_tags) INTERSECT "
    "SELECT unnest(CAST(:tags AS text[])))) DESC, a.kb_ref"
)
FULL_TEXT = text(
    "SELECT c.article_id, max(ts_rank(c.tsv, q)) AS rank "
    "FROM agent_kb_chunks c JOIN agent_kb_articles a ON a.id = c.article_id, "
    "to_tsquery('english', :query) q "
    "WHERE a.status = 'published' AND a.audience = 'store_staff' AND c.tsv @@ q "
    "AND (CAST(:element_type AS text) IS NULL OR CAST(:element_type AS text) = ANY(a.applies_to)) "
    "GROUP BY c.article_id ORDER BY rank DESC LIMIT :limit"
)
VECTOR = text(
    "SELECT c.article_id, max(1 - (c.embedding <=> CAST(CAST(:vector AS text) AS vector))) "
    "AS similarity FROM agent_kb_chunks c JOIN agent_kb_articles a ON a.id = c.article_id "
    "WHERE a.status = 'published' AND a.audience = 'store_staff' "
    "AND c.embedding_model = :model AND c.embedding IS NOT NULL "
    "AND (CAST(:element_type AS text) IS NULL OR CAST(:element_type AS text) = ANY(a.applies_to)) "
    "GROUP BY c.article_id ORDER BY similarity DESC LIMIT :limit"
)
ARTICLES = text(
    "SELECT a.id, a.kb_ref, a.version, a.title, a.summary FROM agent_kb_articles a "
    "WHERE a.id = ANY(CAST(:ids AS uuid[]))"
)


@dataclass(frozen=True)
class ArticleCandidate:
    article_id: uuid.UUID
    kb_ref: str
    version: int
    title: str
    summary: str
    score: float
    matched_by: MatchedBy


@dataclass
class SearchResult:
    candidates: list[ArticleCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def tsquery_for(words: str) -> str | None:
    """'Printer is jammed!' -> 'printer | jammed' (any word may match; safe characters only)."""
    terms = [w for w in re.findall(r"[a-z0-9]+", words.lower()) if w not in STOPWORDS]
    unique = list(dict.fromkeys(terms))[:12]
    return " | ".join(unique) or None


def rrf(rankings: Sequence[Sequence[uuid.UUID]], k: int) -> dict[uuid.UUID, float]:
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, article_id in enumerate(ranking, start=1):
            scores[article_id] = scores.get(article_id, 0.0) + 1.0 / (k + rank)
    return scores


def _candidates(
    rows: Sequence[Row[Any]], score: float, matched_by: MatchedBy
) -> list[ArticleCandidate]:
    return [
        ArticleCandidate(row.id, row.kb_ref, row.version, row.title, row.summary, score, matched_by)
        for row in rows
    ]


async def find_articles(
    session: AsyncSession,
    *,
    element_type: str | None = None,
    fault_code: str | None = None,
    kb_tags: Sequence[str] = (),
    query: str | None = None,
    embedder: Embedder | None = None,
    top_k: int = 5,
    min_similarity: float = 0.55,
    rrf_k: int = 60,
) -> SearchResult:
    result = SearchResult()
    if fault_code:
        rows = (
            await session.execute(EXACT, {"element_type": element_type, "fault_code": fault_code})
        ).all()
        if rows:
            result.candidates = _candidates(rows, 1.0, "exact")[:top_k]
            return result
    if kb_tags:
        rows = (
            await session.execute(TAGS, {"element_type": element_type, "tags": list(kb_tags)})
        ).all()
        if rows:
            result.candidates = _candidates(rows, 0.9, "tag")[:top_k]
            return result
    if not query or not query.strip():
        return result

    rankings: list[list[uuid.UUID]] = []
    tsquery = tsquery_for(query)
    if tsquery:
        rows = await session.execute(
            FULL_TEXT, {"query": tsquery, "element_type": element_type, "limit": top_k}
        )
        rankings.append([row.article_id for row in rows])
    if embedder is not None:
        try:
            [vector] = await embedder.embed([query], "query")
        except EmbeddingError as exc:
            result.warnings.append(f"vector search skipped: {exc}")
        else:
            rows = await session.execute(
                VECTOR,
                {
                    "vector": vector_literal(vector),
                    "model": embedder.model_key,
                    "element_type": element_type,
                    "limit": top_k,
                },
            )
            rankings.append([r.article_id for r in rows if r.similarity >= min_similarity])
    scores = rrf(rankings, rrf_k)
    if not scores:
        return result
    rows = {row.id: row for row in await session.execute(ARTICLES, {"ids": list(scores)})}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], rows[item[0]].kb_ref))
    result.candidates = [
        ArticleCandidate(
            article_id,
            rows[article_id].kb_ref,
            rows[article_id].version,
            rows[article_id].title,
            rows[article_id].summary,
            round(score, 6),
            "search",
        )
        for article_id, score in ranked[:top_k]
    ]
    return result
