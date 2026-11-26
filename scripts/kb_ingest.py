# scripts/kb_ingest.py
"""Validate and publish the KB articles (kb/articles/*.yaml).

    uv run python -m scripts.kb_ingest --check              validate only, no database
    uv run python -m scripts.kb_ingest --publish            validate, embed, publish
    uv run python -m scripts.kb_ingest --publish --reembed  recompute every embedding
                                                            (after changing the provider)

All or nothing: any problem in any article -> nothing is published. Publishes as
ric_kb_admin (KB_ADMIN_DATABASE_URL), in one transaction. Embeddings use
KB_EMBEDDING_PROVIDER (none = tags + full-text search only).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.agentic.config import get_agentic_settings
from app.agentic.kb import articles_path, limits
from app.agentic.kb.articles import coverage_gaps, load_articles, validate_articles
from app.agentic.kb.embeddings import EmbeddingError, build_embedder
from app.agentic.kb.ingest import publish_articles
from app.agentic.store_graph import load_configured_catalogue
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_session_factory, session_scope


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate only")
    mode.add_argument("--publish", action="store_true", help="validate and publish")
    parser.add_argument("--reembed", action="store_true", help="recompute all embeddings")
    parser.add_argument(
        "--allow-coverage-gaps", action="store_true", help="do not fail on missing coverage"
    )
    args = parser.parse_args()
    settings = get_agentic_settings()
    catalogue = load_configured_catalogue(settings)
    folder = articles_path(settings)
    articles, problems = load_articles(folder)
    problems += validate_articles(
        articles, catalogue, limits(settings), allow_coverage_gaps=args.allow_coverage_gaps
    )
    print(f"articles: {folder} ({len(articles)} files)")
    for problem in problems:
        print(f"[FAIL] {problem}")
    if args.allow_coverage_gaps:
        for gap in coverage_gaps(articles, catalogue):
            print(f"[WARN] {gap}")
    if problems:
        print(f"{len(problems)} problems - nothing published")
        return 1
    for article in articles:
        print(f"[OK] {article.kb_ref} {article.title} ({len(article.steps)} steps)")
    if args.check:
        print("check passed")
        return 0

    if settings.kb_admin_database_url is None:
        print("[FAIL] KB_ADMIN_DATABASE_URL is not set in .env", file=sys.stderr)
        return 1
    try:
        embedder = build_embedder(settings, get_settings())
    except EmbeddingError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    engine = create_async_engine(settings.kb_admin_database_url, poolclass=NullPool)
    try:
        async with session_scope(create_session_factory(engine)) as session:
            report = await publish_articles(
                session,
                articles,
                catalogue,
                embedder=embedder,
                now=SystemClock().now(),
                reembed=args.reembed,
            )
    except EmbeddingError as exc:
        print(f"[FAIL] {exc} - nothing published", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"embedding model: {report.embedding_model or 'none (tags + full-text only)'}")
    for label, items in (
        ("added", report.added),
        ("changed", report.changed),
        ("re-embedded", report.reembedded),
        ("unchanged", report.unchanged),
        ("retired", report.retired),
    ):
        if items:
            print(f"{label}: {', '.join(items)}")
    print(
        f"chunks written {report.chunks_written}, vectors computed {report.vectors_computed}, "
        f"reused {report.vectors_reused}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
