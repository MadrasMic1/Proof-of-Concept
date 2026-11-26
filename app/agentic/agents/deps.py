# app/agentic/agents/deps.py
"""Build AgentDeps: model targets (primary, fallback), cached chat clients with the
metered transport, usage rows written with the given session factory."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.agents.runtime import AgentDeps
from app.agentic.config import AgenticSettings
from app.agentic.llm.client import (
    ModelTarget,
    build_chat_client,
    fallback_target,
    primary_target,
)
from app.agentic.llm.usage import database_recorder
from app.agentic.tools.policy import ToolPolicy
from app.core.config import Settings


def build_agent_deps(
    settings: AgenticSettings,
    core: Settings,
    policy: ToolPolicy,
    mcp_url: str,
    *,
    usage_factory: async_sessionmaker[AsyncSession] | None,
    inner: Any | None = None,
    targets: list[ModelTarget] | None = None,
) -> AgentDeps:
    """inner: a scripted model transport (tests). targets: override (tests)."""
    if targets is None:
        targets = [primary_target(settings, core, "specialist")]
        fallback = fallback_target(settings, core)
        if fallback is not None:
            targets.append(fallback)
    recorder = database_recorder(usage_factory) if usage_factory is not None else None
    cache: dict[str, Any] = {}

    def chat_client(target: ModelTarget) -> Any:
        key = f"{target.label}|{target.fallback}"
        if key not in cache:
            cache[key] = build_chat_client(
                target, settings, "specialist", recorder=recorder, inner=inner
            )
        return cache[key]

    return AgentDeps(
        settings=settings,
        policy=policy,
        mcp_url=mcp_url,
        targets=targets,
        chat_client=chat_client,
    )
