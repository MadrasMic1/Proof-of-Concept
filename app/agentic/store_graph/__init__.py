# app/agentic/store_graph/__init__.py
"""The store graph: catalogue, in-memory graph, generic engine and findings."""

from __future__ import annotations

from pathlib import Path

from app.agentic.config import AgenticSettings
from app.agentic.store_graph.catalogue import Catalogue, CatalogueError, load_catalogue

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def catalogue_path(settings: AgenticSettings) -> Path:
    path = Path(settings.store_graph_catalogue_path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_configured_catalogue(settings: AgenticSettings) -> Catalogue:
    return load_catalogue(catalogue_path(settings))


__all__ = ["Catalogue", "CatalogueError", "catalogue_path", "load_configured_catalogue"]
