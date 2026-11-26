# app/agentic/drafts/__init__.py
"""Incident drafts: triage baseline, duplicate check, validated drafts (no LLM).

The Incident Desk agent (Agentic Phase 8) proposes text and values; this package
decides what is allowed. Nothing here creates an incident: that happens only after
a store manager taps Confirm (Agentic Phase 10), through app/services/incidents.py.
"""
