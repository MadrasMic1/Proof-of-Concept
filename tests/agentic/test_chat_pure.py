# tests/agentic/test_chat_pure.py
"""Agentic Phase 7: composer, claim check, specialist summaries, state text (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.agentic.agents.schemas import SpecialistResult
from app.agentic.chat.composer import NOTHING_RAISED, check_claims, compose
from app.agentic.chat.orchestrator import diagnosis_of, summarise
from app.agentic.chat.schemas import AssistantResponse
from app.agentic.chat.state import DerivedState, DraftInfo, GuideInfo, state_block
from app.agentic.values import ConversationState

NOW = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)
NODE = {"kind": "device", "id": str(uuid.uuid4()), "name": "RF Gun 02",
        "element_type": "rf_gun", "label": "RF gun RF Gun 02"}  # fmt: skip
CAUSE = {
    "finding_id": "F-1a2b3c4d",
    "certainty": "confirmed",
    "root_node": NODE,
    "status": "degraded",
    "fault_code": "BATTERY_LOW",
    "severity": "minor",
    "impacted_nodes": [],
    "evidence_path": [],
    "explanation": "RF gun RF Gun 02 works intermittently.",
    "staff_can_fix": True,
    "kb_tags": ["rf-battery"],
    "covered_by_incident": None,
    "in_maintenance": False,
}
STEP = {
    "guide_id": str(uuid.uuid4()),
    "kb_ref": "KB-RF-001",
    "kb_version": 1,
    "title": "RF gun low battery",
    "step_id": "S1",
    "step_no": 1,
    "instruction": "Put the RF gun in its charging cradle.",
    "ask": "Is the charging light on?",
    "options": [{"key": "charging", "label": "Yes, it is charging"},
                {"key": "not_charging", "label": "No charging light"}],
    "state_version": 1,
}  # fmt: skip
DRAFT = DraftInfo(uuid.uuid4(), 2, NOW, "RF Gun 02 - battery does not charge", 5)


def _result(agent: str, outcome: str, body: dict[str, Any], **kw: Any) -> SpecialistResult:
    return SpecialistResult(agent=agent, status=kw.pop("status", "ok"), outcome=outcome,
                            summary=kw.pop("summary", "s"), body=body, **kw)  # fmt: skip


def _compose(results: list[SpecialistResult], state: DerivedState, **kw: Any) -> AssistantResponse:
    values: dict[str, Any] = {
        "session_id": uuid.uuid4(),
        "turn_no": 1,
        "correlation_id": uuid.uuid4(),
        "answer": "Here you go.",
        "requested_cards": [],
        "suggested_replies": ["a", "b", "c", "d"],
        "results": results,
        "state": state,
        "role": "store_manager",
        "as_of": NOW,
        "stale": False,
        "warnings": [],
        "max_replies": 3,
    }
    values.update(kw)
    return compose(**values)


def test_claim_check() -> None:
    idle = ConversationState.IDLE
    text, changed = check_claims("I have raised INC00009999 for you. Tap Confirm.", idle)
    assert changed and text == f"{NOTHING_RAISED} Tap Confirm."
    for fine in ("This was already raised as INC00001234.", "Network Ops opened it."):
        assert check_claims(fine, idle) == (fine, False)
    raised = ConversationState.INCIDENT_RAISED
    assert check_claims("I've raised it.", raised) == ("I've raised it.", False)


def test_cards_come_only_from_data() -> None:
    insight = _result("store_insight", "cause_found", {"causes": [CAUSE], "device_options": []})
    guide = _result("kb_guide", "needs_user_input", {"step": STEP, "guide_id": STEP["guide_id"]})
    response = _compose(
        [insight, guide],
        DerivedState(ConversationState.GUIDING),
        requested_cards=["finding", "incident_draft"],
    )
    assert [c.kind for c in response.cards] == ["finding", "kb_step"]
    assert response.suggested_replies == ["Yes, it is charging", "No charging light"]
    plain = _compose([], DerivedState(ConversationState.IDLE), requested_cards=["finding"])
    assert plain.cards == [] and plain.suggested_replies == ["a", "b", "c"]


def test_pending_action_only_from_state_and_for_managers() -> None:
    pending = DerivedState(ConversationState.DRAFT_PENDING, draft=DRAFT)
    manager = _compose([], pending)
    assert manager.pending_action is not None
    assert manager.pending_action.draft_id == str(DRAFT.id)
    assert manager.pending_action.version == 2
    assert _compose([], pending, role="store_staff").pending_action is None
    assert _compose([], DerivedState(ConversationState.IDLE)).pending_action is None


def test_errors_become_warnings() -> None:
    failed = _result("kb_guide", "invalid_result", {}, status="error", summary="no result")
    response = _compose([failed], DerivedState(ConversationState.IDLE))
    assert "kb_guide: no result" in response.warnings


def test_summaries_and_diagnosis() -> None:
    insight = _result("store_insight", "cause_found", {"causes": [CAUSE], "device_options": []})
    seen = summarise(insight)
    assert seen["causes"][0]["fault"] == "BATTERY_LOW" and "id" not in str(seen["causes"])
    diagnosis = diagnosis_of(insight)
    assert diagnosis["cause"]["element_type"] == "rf_gun"
    assert diagnosis["cause"]["kb_tags"] == ["rf-battery"]
    guide = summarise(_result("kb_guide", "needs_user_input", {"step": STEP}))
    assert guide["step"]["options"] == ["Yes, it is charging", "No charging light"]


def test_state_block() -> None:
    guide = GuideInfo(uuid.uuid4(), "KB-RF-001", "RF gun low battery", "in_progress", "S3",
                      "Is the charging light on now?")  # fmt: skip
    text = state_block(DerivedState(ConversationState.GUIDING, guide=guide), None, "store_staff")
    assert "GUIDING" in text and "step S3" in text and "store_staff" in text
    assert str(guide.id) not in text
    pending = state_block(DerivedState(ConversationState.DRAFT_PENDING, draft=DRAFT),
                          {"summary": "RF Gun 02 battery low."}, "store_manager")  # fmt: skip
    assert "tap Confirm" in pending and "Last diagnosis" in pending
