# app/agentic/chat/composer.py
"""Build the AssistantResponse: text from the model, cards from data.

- Cards come only from THIS turn's specialist results; the model only names the card
  kinds it wants. kb_step and incident_draft are added whenever there is data for them.
- suggested_replies: a kb_step card uses its own option labels; otherwise the model's
  (at most AGENT_MAX_SUGGESTED_REPLIES).
- pending_action only from the derived state (draft_pending) and only for a store manager.
- Claim check: a sentence claiming the assistant raised / created / logged an incident
  is replaced unless the state is incident_raised.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.agentic.agents.schemas import SpecialistResult
from app.agentic.chat.schemas import AssistantResponse, Card, Freshness, PendingAction
from app.agentic.chat.state import DerivedState
from app.agentic.values import ConversationState

NOTHING_RAISED = "Nothing has been raised yet."
_DONE = r"(raised|created|logged|opened|submitted|filed)"
_CLAIM = re.compile(
    rf"\b(I(?:'ve| have)?|we(?:'ve| have)?)\s+(?:just\s+)?{_DONE}\b"
    rf"|\b(has|have)\s+been\s+{_DONE}\s+for\s+you\b"
    rf"|\byour\s+(incident|ticket)\s+(has\s+been|is|was)\s+{_DONE}\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
AUTO_CARDS = ("kb_step", "incident_draft", "duplicate_notice", "device_options")


def check_claims(answer: str, state: ConversationState) -> tuple[str, bool]:
    if state == ConversationState.INCIDENT_RAISED:
        return answer, False
    changed = False
    out: list[str] = []
    for sentence in _SENTENCE.split(answer.strip()):
        if _CLAIM.search(sentence):
            changed = True
            if NOTHING_RAISED not in out:
                out.append(NOTHING_RAISED)
        else:
            out.append(sentence)
    return " ".join(out), changed


def _latest(results: Sequence[SpecialistResult], agent: str) -> SpecialistResult | None:
    for result in reversed(results):
        if result.agent == agent and result.status == "ok":
            return result
    return None


def available_cards(results: Sequence[SpecialistResult]) -> dict[str, Card]:
    cards: dict[str, Card] = {}
    insight = _latest(results, "store_insight")
    if insight is not None:
        causes = insight.body.get("causes") or []
        if causes:
            cards["finding"] = Card(
                kind="finding",
                title=causes[0]["root_node"]["label"],
                data={"outcome": insight.outcome, "causes": causes},
            )
        options = insight.body.get("device_options") or []
        if options:
            cards["device_options"] = Card(
                kind="device_options",
                title="Which device do you mean?",
                data={"options": options},
            )
    guide = _latest(results, "kb_guide")
    if guide is not None:
        step = guide.body.get("step")
        if step and guide.outcome == "needs_user_input":
            cards["kb_step"] = Card(kind="kb_step", title=step["title"], data=step)
        candidates = guide.body.get("candidates") or []
        if candidates:
            cards["kb_choices"] = Card(
                kind="kb_choices", title="Which of these fits?", data={"candidates": candidates}
            )
    desk = _latest(results, "incident_desk")
    if desk is not None:
        incidents = desk.body.get("incidents") or []
        if incidents and desk.outcome != "duplicate_found":
            cards["incident_list"] = Card(
                kind="incident_list", title="Incidents", data={"incidents": incidents}
            )
        draft = desk.body.get("draft")
        if draft and desk.outcome in ("draft_ready", "draft_revised"):
            cards["incident_draft"] = Card(
                kind="incident_draft", title=draft["short_description"], data=draft
            )
        duplicates = desk.body.get("duplicates")
        if desk.outcome in ("duplicate_found", "possible_duplicate") and duplicates:
            cards["duplicate_notice"] = Card(
                kind="duplicate_notice",
                title=(
                    "Already being handled"
                    if desk.outcome == "duplicate_found"
                    else "Possibly already reported"
                ),
                data=duplicates,
            )
    return cards


def compose(
    *,
    session_id: uuid.UUID,
    turn_no: int | None,
    correlation_id: uuid.UUID,
    answer: str,
    requested_cards: Sequence[str],
    suggested_replies: Sequence[str],
    results: Sequence[SpecialistResult],
    state: DerivedState,
    role: str,
    as_of: datetime | None,
    stale: bool,
    warnings: Sequence[str],
    max_replies: int,
) -> AssistantResponse:
    text, changed = check_claims(answer or "", state.state)
    notes = list(warnings)
    if changed:
        notes.append("an unsupported claim about raising an incident was removed")
    available = available_cards(results)
    kinds = [k for k in requested_cards if k in available]
    kinds += [k for k in AUTO_CARDS if k in available and k not in kinds]
    cards = [available[k] for k in dict.fromkeys(kinds)]
    replies = [r.strip() for r in suggested_replies if r and r.strip()]
    step = available.get("kb_step")
    if step is not None:
        replies = [o["label"] for o in step.data.get("options", [])]
    pending = None
    if (
        state.state == ConversationState.DRAFT_PENDING
        and state.draft is not None
        and role == "store_manager"
    ):
        pending = PendingAction(
            draft_id=str(state.draft.id),
            version=state.draft.version,
            expires_at=state.draft.expires_at,
            short_description=state.draft.short_description,
            priority=state.draft.priority,
        )
    for result in results:
        if result.status != "ok":
            notes.append(f"{result.agent}: {result.summary}")
        if result.trace.fallback_used:
            notes.append(f"{result.agent}: fallback model used")
    return AssistantResponse(
        session_id=str(session_id),
        turn_no=turn_no,
        correlation_id=str(correlation_id),
        answer=text or "Sorry, I could not answer that.",
        cards=cards,
        suggested_replies=replies if step is not None else replies[:max_replies],
        pending_action=pending,
        state=str(state.state),
        freshness=Freshness(as_of=as_of, stale_warning=stale),
        warnings=list(dict.fromkeys(notes)),
    )


def card_titles(response: dict[str, Any]) -> str:
    titles = [f"{c['kind']}: {c['title']}" for c in response.get("cards") or []]
    return f" [cards: {'; '.join(titles)}]" if titles else ""
