# app/agentic/kb/__init__.py
"""Knowledge base: structured articles, ingestion, retrieval and guide state."""

from __future__ import annotations

from pathlib import Path

from app.agentic.config import AgenticSettings
from app.agentic.kb.articles import Limits
from app.agentic.store_graph import PROJECT_ROOT


def articles_path(settings: AgenticSettings) -> Path:
    path = Path(settings.kb_articles_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def limits(settings: AgenticSettings) -> Limits:
    return Limits(
        instruction=settings.kb_instruction_max_chars,
        ask=settings.kb_ask_max_chars,
        label=settings.kb_label_max_chars,
    )
