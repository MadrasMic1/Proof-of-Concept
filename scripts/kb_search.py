# scripts/kb_search.py
"""Search the published KB the way KB Guide will (Agentic Phase 3 check tool).

    uv run python -m scripts.kb_search --type printer --fault PAPER_JAM
    uv run python -m scripts.kb_search --type rf_gun --tags rf-battery
    uv run python -m scripts.kb_search --text "receipt comes out blank"
    uv run python -m scripts.kb_search --embed-check      one embedding call, prints the size

Read-only (the app login). Prints how each article matched: exact, tag or search.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.agentic.config import get_agentic_settings
from app.agentic.kb.embeddings import EmbeddingError, build_embedder
from app.agentic.kb.retrieval import find_articles
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory


async def embed_check() -> int:
    settings = get_agentic_settings()
    try:
        embedder = build_embedder(settings, get_settings())
        if embedder is None:
            print("KB_EMBEDDING_PROVIDER=none: no embeddings (tags + full-text only)")
            return 0
        [query] = await embedder.embed(["receipt printer paper jam"], "query")
        [passage] = await embedder.embed(["Clear a jammed receipt printer"], "passage")
    except EmbeddingError as exc:
        print(f"[FAIL] {exc}")
        return 1
    padding = 0
    for value in reversed(query):
        if value != 0.0:
            break
        padding += 1
    similarity = sum(a * b for a, b in zip(query, passage, strict=True))
    norm = (sum(a * a for a in query) ** 0.5) * (sum(b * b for b in passage) ** 0.5)
    print(
        f"[OK] {embedder.model_key}: {len(query) - padding} values from the model, "
        f"padded to {len(query)}; cosine(query, passage) = {similarity / norm:.3f}"
    )
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--type", help="element type, e.g. printer")
    parser.add_argument("--fault", help="fault code, e.g. PAPER_JAM")
    parser.add_argument("--tags", help="comma-separated kb tags")
    parser.add_argument("--text", help="the user's words")
    parser.add_argument("--embed-check", action="store_true", help="test the embedding API")
    args = parser.parse_args()
    if args.embed_check:
        return await embed_check()
    settings = get_agentic_settings()
    try:
        embedder = build_embedder(settings, get_settings()) if args.text else None
    except EmbeddingError as exc:
        print(f"[WARN] {exc}: full-text only")
        embedder = None
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            result = await find_articles(
                session,
                element_type=args.type,
                fault_code=args.fault,
                kb_tags=[t.strip() for t in (args.tags or "").split(",") if t.strip()],
                query=args.text,
                embedder=embedder,
                top_k=settings.kb_search_top_k,
                min_similarity=settings.kb_search_min_score,
                rrf_k=settings.kb_search_rrf_k,
            )
    finally:
        await engine.dispose()
    for warning in result.warnings:
        print(f"[WARN] {warning}")
    for c in result.candidates:
        print(f"{c.score:.4f} {c.matched_by:6} {c.kb_ref} v{c.version} - {c.title}")
    if not result.candidates:
        print("no article found")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
