# app/agentic/drafts/mapping.py
"""A confirmed draft -> the input of the EXISTING incident service.

Agentic Phase 10 (Confirm) will call:
    incidents.create_incident(session, actor, draft.store_id, draft_to_new_incident(draft), now)
with an ActorContext for the store manager and source "agent". Built and tested now
so the draft fields provably fit app/services/incidents.NewIncident (not changed).
"""

from __future__ import annotations

from app.core.actor import ActorContext
from app.models import AgentIncidentDraft
from app.models.values import ActorType, AuditSource, ContactType
from app.services.incidents import NewIncident


def draft_to_new_incident(draft: AgentIncidentDraft) -> NewIncident:
    return NewIncident(
        short_description=draft.short_description,
        category=draft.category,
        impact=draft.impact,
        urgency=draft.urgency,
        contact_type=ContactType.SELF_SERVICE,
        description=draft.description,
        u_symptom=draft.u_symptom,
        subcategory=draft.subcategory,
        caller=draft.user_id,
        network_element_id=draft.cmdb_ci_network_element_id,
        device_id=draft.cmdb_ci_device_id,
        assignment_group_id=draft.assignment_group_id,
    )


def store_manager_actor(user_id: str) -> ActorContext:
    """The store manager, acting through the assistant (audit source 'agent')."""
    return ActorContext(actor_type=ActorType.STORE_USER, actor=user_id, source=AuditSource.AGENT)
