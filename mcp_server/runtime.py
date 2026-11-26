# mcp_server/runtime.py
"""Build the tool runtime for the MCP process: own engine as ric_mcp, policy,
catalogue, embedder. Refuses to start when anything is inconsistent."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.agentic.config import AgenticSettings
from app.agentic.kb.embeddings import EmbeddingError, build_embedder
from app.agentic.store_graph import PROJECT_ROOT, load_configured_catalogue
from app.agentic.tools.pipeline import ToolRuntime
from app.agentic.tools.policy import ToolPolicy, load_policy
from app.agentic.tools.registry import TOOLS
from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.db.session import create_session_factory

logger = logging.getLogger("ric.mcp")


class StartupError(RuntimeError):
    pass


def policy_path(settings: AgenticSettings) -> Path:
    path = Path(settings.mcp_tool_policy_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_checked_policy(settings: AgenticSettings) -> ToolPolicy:
    policy = load_policy(policy_path(settings))
    problems = policy.problems_against(TOOLS)
    if problems:
        raise StartupError("tool policy does not match the tools: " + "; ".join(problems))
    return policy


def create_mcp_engine(settings: AgenticSettings, url: str | None = None) -> AsyncEngine:
    database_url = url or settings.mcp_database_url
    if database_url is None:
        raise StartupError("MCP_DATABASE_URL is not set in .env")
    return create_async_engine(
        database_url,
        pool_size=settings.mcp_db_pool_size,
        max_overflow=settings.mcp_db_max_overflow,
        pool_timeout=settings.mcp_db_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "statement_timeout": str(settings.mcp_db_statement_timeout_ms),
                "application_name": "ric-mcp",
            }
        },
    )


def create_runtime(
    settings: AgenticSettings,
    core: Settings | None,
    engine: AsyncEngine,
    *,
    clock: Clock | None = None,
    with_embedder: bool = True,
) -> ToolRuntime:
    secret = settings.agent_user_context_token_secret
    if secret is None or not secret.get_secret_value().strip():
        raise StartupError("AGENT_USER_CONTEXT_TOKEN_SECRET is not set in .env")
    embedder = None
    if with_embedder and core is not None:
        try:
            embedder = build_embedder(settings, core)
        except EmbeddingError as exc:
            logger.warning("KB vector search off: %s", exc)
    return ToolRuntime(
        settings=settings,
        catalogue=load_configured_catalogue(settings),
        policy=load_checked_policy(settings),
        factory=create_session_factory(engine),
        clock=clock or SystemClock(),
        embedder=embedder,
    )
