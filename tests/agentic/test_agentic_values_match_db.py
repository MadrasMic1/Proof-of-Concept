# tests/agentic/test_agentic_values_match_db.py
"""Every value list in app/agentic/values.py equals the CHECK constraint of migration 0005.

Same method as tests/test_values_match_db.py, which stays unchanged.
"""

from __future__ import annotations

import re
from enum import Enum

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic import values as av
from app.models.values import IncidentCategory

VALUE_LISTS: list[tuple[str, str, type[Enum]]] = [
    ("agent_store_dependencies", "from_kind", av.DependencySourceKind),
    ("agent_store_dependencies", "to_kind", av.DependencyTargetKind),
    ("agent_store_dependencies", "relation", av.DependencyRelation),
    ("agent_chat_sessions", "role", av.AgentRole),
    ("agent_chat_sessions", "status", av.ChatSessionStatus),
    ("agent_chat_sessions", "state", av.ConversationState),
    ("agent_chat_messages", "role", av.MessageRole),
    ("agent_kb_articles", "status", av.KbArticleStatus),
    ("agent_kb_articles", "audience", av.KbAudience),
    ("agent_kb_chunks", "chunk_type", av.KbChunkType),
    ("agent_kb_guide_sessions", "status", av.GuideStatus),
    ("agent_incident_drafts", "status", av.DraftStatus),
    ("agent_incident_drafts", "category", IncidentCategory),
    ("agent_llm_calls", "agent", av.AgentName),
    ("agent_llm_calls", "profile", av.LlmProfile),
    ("agent_llm_calls", "status", av.LlmCallStatus),
    ("agent_tool_calls", "role", av.AgentRole),
    ("agent_tool_calls", "agent", av.SpecialistName),
    ("agent_tool_calls", "outcome", av.ToolOutcome),
]

CHECKS_SQL = text(
    """
    SELECT pg_get_constraintdef(c.oid)
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND t.relname = :table
    """
)


def _allowed_values(definitions: list[str], column: str) -> set[str]:
    """Extract the list from PostgreSQL's form: (col = ANY (ARRAY['a'::text, 'b'::text]))."""
    pattern = re.compile(rf'\(\(?"?{column}"?\)? = ANY \(\(?ARRAY\[(.*?)\]')
    for definition in definitions:
        match = pattern.search(definition)
        if match:
            items = [item.strip() for item in match.group(1).split(",")]
            return {re.sub(r"::\w+(\s\w+)?$", "", item).strip("'") for item in items}
    raise AssertionError(f"no 'IN (...)' check found for column {column}")


@pytest.mark.parametrize(
    ("table", "column", "enum"),
    VALUE_LISTS,
    ids=[f"{t}.{c}" for t, c, _ in VALUE_LISTS],
)
async def test_value_list_matches_check(
    db_session: AsyncSession, table: str, column: str, enum: type[Enum]
) -> None:
    definitions = list((await db_session.scalars(CHECKS_SQL, {"table": table})).all())
    assert _allowed_values(definitions, column) == {str(member.value) for member in enum}


def test_error_codes_are_outcomes() -> None:
    assert {c.value for c in av.ErrorCode} | {"ok"} == {o.value for o in av.ToolOutcome}
