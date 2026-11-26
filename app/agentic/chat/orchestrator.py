# app/agentic/chat/orchestrator.py
"""The orchestrator: one Agent Framework agent with four local tools.

    ask_store_insight   run Store Insight
    ask_kb_guide        run KB Guide; CODE fills the guide id (continue) and the
                        element type / fault code / kb tags / subject / finding ids (start)
    ask_incident_desk   run Incident Desk; CODE fills finding id, ended guide and pending
                        draft; draft / revise refused for store_staff
    submit_answer       the final answer (text, card kinds, suggested replies)

Limits: AGENT_ORCH_MAX_SPECIALIST_CALLS per turn, AGENT_ORCH_MAX_CALLS_PER_SPECIALIST each,
AGENT_ORCH_MAX_MODEL_CALLS orchestrator model calls (transport). A refused call returns a
short text to the model (never an exception: the framework would retry it). The model
sees SUMMARIES of specialist results; the full results stay here for the cards.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import agent_framework as af
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agentic.agents.bridge import make_tool
from app.agentic.agents.runtime import AgentDeps, is_unavailable, run_specialist
from app.agentic.agents.schemas import (
    IncidentDeskBrief,
    KbGuideBrief,
    SpecialistResult,
    StoreInsightBrief,
    TrustedContext,
)
from app.agentic.chat.schemas import CardKind
from app.agentic.chat.state import DerivedState
from app.agentic.config import AgenticSettings
from app.agentic.llm.client import ModelTarget
from app.agentic.llm.transport import CURRENT_RUN, RunState
from app.agentic.values import ConversationState

PROMPT = Path(__file__).resolve().parents[1] / "agents" / "prompts" / "orchestrator.v1.md"
HANDOFF = ("exhausted", "needs_technician")


class _Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AskStoreInsight(_Args):
    question: str = Field(min_length=1, max_length=500, description="the user's problem")
    devices_mentioned: list[str] = Field(
        default_factory=list, max_length=5, description="devices in the user's own words"
    )
    user_reported: list[str] = Field(
        default_factory=list, max_length=5, description="what the user says they tried"
    )


class AskKbGuide(_Args):
    mode: Literal["start", "continue"] = Field(description="start a guide or continue one")
    issue: str | None = Field(default=None, max_length=500, description="the problem")
    user_report: str | None = Field(
        default=None, max_length=500, description="the user's answer to the current step"
    )


class AskIncidentDesk(_Args):
    mode: Literal["lookup", "draft", "revise"]
    issue: str = Field(min_length=1, max_length=500, description="the problem, short")
    user_words: str | None = Field(default=None, max_length=500)
    revision_request: str | None = Field(default=None, max_length=500)
    manager_accepted_possible_duplicate: bool = False


class SubmitAnswer(_Args):
    answer: str = Field(min_length=1, max_length=1500, description="what the user reads")
    show_cards: list[CardKind] = Field(default_factory=list, max_length=4)
    suggested_replies: list[str] = Field(default_factory=list, max_length=5)


@dataclass
class TurnContext:
    trusted: TrustedContext  # store, user, role, chat, correlation (no guide / draft ids)
    start_state: DerivedState
    state_data: dict[str, Any]


@dataclass
class OrchestratorOutcome:
    answer: dict[str, Any] | None
    text: str
    results: list[SpecialistResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model_calls: int = 0


def _values(model: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    raw = values.get("arguments") if set(values) == {"arguments"} else values
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    fields = model.model_fields
    return {k: v for k, v in dict(raw or {}).items() if k in fields and v is not None}


def summarise(result: SpecialistResult) -> dict[str, Any]:
    """What the orchestrator reads about a specialist result (no ids)."""
    out: dict[str, Any] = {
        "status": result.status,
        "outcome": result.outcome,
        "summary": result.summary,
        "suggested_next": result.suggested_next,
    }
    body = result.body
    if result.agent == "store_insight":
        out["causes"] = [
            {
                "item": c["root_node"]["label"],
                "status": c["status"],
                "fault": c.get("fault_code"),
                "explanation": c["explanation"],
                "staff_can_fix": c["staff_can_fix"],
                "already_reported_as": c.get("covered_by_incident"),
            }
            for c in body.get("causes") or []
        ]
        out["device_options"] = [n["label"] for n in body.get("device_options") or []]
    elif result.agent == "kb_guide":
        step = body.get("step")
        if step:
            out["step"] = {
                "article": step["title"],
                "step_no": step["step_no"],
                "instruction": step["instruction"],
                "ask": step["ask"],
                "options": [o["label"] for o in step.get("options", [])],
            }
        out["articles"] = [c["title"] for c in body.get("candidates") or []]
        out["steps_tried"] = len(body.get("steps_tried") or [])
    elif result.agent == "incident_desk":
        out["incidents"] = [
            {
                "number": i.get("number") or i.get("incident_number"),
                "title": i.get("short_description"),
                "state": i.get("state_label"),
                "eta": i.get("eta") or i.get("eta_note"),
            }
            for i in body.get("incidents") or []
        ]
        draft = body.get("draft")
        if draft:
            out["draft"] = {
                "title": draft.get("short_description"),
                "priority": draft.get("priority"),
                "team": draft.get("assignment_group"),
                "expires_at": draft.get("expires_at"),
            }
    if result.problems:
        out["problems"] = result.problems[:3]
    return out


class Orchestrator:
    def __init__(
        self,
        *,
        settings: AgenticSettings,
        agent_deps: AgentDeps,
        targets: list[ModelTarget],
        client_for: Callable[[ModelTarget], Any],
    ) -> None:
        self.settings = settings
        self.agent_deps = agent_deps
        self.targets = targets
        self.client_for = client_for

    # --- id filling ------------------------------------------------------------------

    @staticmethod
    def _diagnosis(results: list[SpecialistResult], ctx: TurnContext) -> dict[str, Any] | None:
        for result in reversed(results):
            if result.agent == "store_insight" and result.status == "ok":
                return diagnosis_of(result)
        return ctx.state_data.get("last_diagnosis")

    @staticmethod
    def _guide_in_progress(results: list[SpecialistResult], ctx: TurnContext) -> uuid.UUID | None:
        for result in reversed(results):
            if result.agent == "kb_guide" and result.status == "ok":
                if result.outcome == "needs_user_input" and result.body.get("guide_id"):
                    return uuid.UUID(result.body["guide_id"])
                return None
        if ctx.start_state.state == ConversationState.GUIDING and ctx.start_state.guide:
            return ctx.start_state.guide.id
        return None

    @staticmethod
    def _handoff_guide(results: list[SpecialistResult], ctx: TurnContext) -> uuid.UUID | None:
        for result in reversed(results):
            ended = result.agent == "kb_guide" and result.outcome in HANDOFF
            if ended and result.status == "ok" and result.body.get("guide_id"):
                return uuid.UUID(result.body["guide_id"])
        start = ctx.start_state
        if start.state == ConversationState.GUIDE_EXHAUSTED and start.guide:
            return start.guide.id
        value = ctx.state_data.get("handoff_guide_id")
        return uuid.UUID(value) if value else None

    @staticmethod
    def _pending_draft(results: list[SpecialistResult], ctx: TurnContext) -> uuid.UUID | None:
        for result in reversed(results):
            draft = result.body.get("draft") if result.agent == "incident_desk" else None
            if draft and result.outcome in ("draft_ready", "draft_revised"):
                return uuid.UUID(draft["draft_id"])
        if ctx.start_state.state == ConversationState.DRAFT_PENDING and ctx.start_state.draft:
            return ctx.start_state.draft.id
        return None

    # --- the run ---------------------------------------------------------------------

    async def run(self, ctx: TurnContext, message: str) -> OrchestratorOutcome:
        targets = list(self.targets)
        target = targets.pop(0)
        outcome = OrchestratorOutcome(answer=None, text="")
        while True:
            try:
                await self._attempt(ctx, message, target, outcome)
                return outcome
            except Exception as exc:
                if is_unavailable(exc) and targets and outcome.answer is None:
                    target = targets.pop(0)
                    outcome.warnings.append("orchestrator: fallback model used")
                    continue
                raise

    async def _attempt(
        self, ctx: TurnContext, message: str, target: ModelTarget, outcome: OrchestratorOutcome
    ) -> None:
        settings = self.settings
        trusted = ctx.trusted
        state = RunState(
            agent="orchestrator",
            profile="orchestrator",
            correlation_id=trusted.correlation_id,
            max_model_calls=settings.agent_orch_max_model_calls,
            store_id=trusted.store_id,
            chat_session_id=trusted.chat_session_id,
            user_id=trusted.user_id,
            fallback_used=target.fallback,
        )
        counts: Counter[str] = Counter()
        results = outcome.results

        def allowed(agent: str) -> str | None:
            if sum(counts.values()) >= settings.agent_orch_max_specialist_calls:
                return "specialist call limit for this turn reached: answer with what you have"
            if counts[agent] >= settings.agent_orch_max_calls_per_specialist:
                return f"{agent} was already asked enough this turn: answer with what you have"
            return None

        async def call(agent: str, brief: BaseModel, extra: dict[str, Any]) -> str:
            refused = allowed(agent)
            if refused:
                return json.dumps({"status": "refused", "summary": refused})
            counts[agent] += 1
            run_trusted = TrustedContext(
                trusted.user_id,
                trusted.role,
                trusted.store_id,
                trusted.correlation_id,
                trusted.chat_session_id,
                guide_id=extra.get("guide_id"),
                draft_id=extra.get("draft_id"),
            )
            try:
                result = await run_specialist(agent, brief, run_trusted, self.agent_deps)
            except Exception as exc:  # the specialist failed hard: tell the model, go on
                outcome.warnings.append(f"{agent} failed: {exc.__class__.__name__}")
                return json.dumps({"status": "error", "summary": f"{agent} is not available"})
            results.append(result)
            return json.dumps(summarise(result), ensure_ascii=False, default=str)

        def parse(model: type[BaseModel], values: dict[str, Any]) -> BaseModel | str:
            try:
                return model.model_validate(_values(model, values))
            except ValidationError as exc:
                return json.dumps({"status": "invalid", "summary": str(exc.errors()[:2])})

        async def ask_store_insight(**values: Any) -> str:
            args = parse(AskStoreInsight, values)
            if isinstance(args, str):
                return args
            assert isinstance(args, AskStoreInsight)
            brief = StoreInsightBrief(
                question=args.question,
                devices_mentioned=args.devices_mentioned,
                user_reported=args.user_reported,
            )
            return await call("store_insight", brief, {})

        async def ask_kb_guide(**values: Any) -> str:
            args = parse(AskKbGuide, values)
            if isinstance(args, str):
                return args
            assert isinstance(args, AskKbGuide)
            if args.mode == "continue":
                guide_id = self._guide_in_progress(results, ctx)
                if guide_id is None:
                    return json.dumps(
                        {"status": "refused", "summary": "no guide is in progress: use start"}
                    )
                brief = KbGuideBrief(mode="continue", user_report=args.user_report or args.issue)
                return await call("kb_guide", brief, {"guide_id": guide_id})
            diagnosis = self._diagnosis(results, ctx) or {}
            cause = diagnosis.get("cause") or {}
            brief = KbGuideBrief(
                mode="start",
                issue=args.issue or diagnosis.get("summary"),
                element_type=cause.get("element_type"),
                fault_code=cause.get("fault_code"),
                kb_tags=cause.get("kb_tags") or [],
                subject_node_id=cause.get("node_id"),
                finding_ids=[cause["finding_id"]] if cause.get("finding_id") else [],
            )
            return await call("kb_guide", brief, {})

        async def ask_incident_desk(**values: Any) -> str:
            args = parse(AskIncidentDesk, values)
            if isinstance(args, str):
                return args
            assert isinstance(args, AskIncidentDesk)
            if args.mode in ("draft", "revise") and trusted.role != "store_manager":
                return json.dumps(
                    {
                        "status": "refused",
                        "summary": "only a store manager can raise or change an incident: "
                        "tell the user to ask their store manager, with the problem and "
                        "the steps already tried",
                    }
                )
            extra: dict[str, Any] = {}
            finding_id = None
            if args.mode == "draft":
                cause = (self._diagnosis(results, ctx) or {}).get("cause") or {}
                finding_id = cause.get("finding_id")
                extra["guide_id"] = self._handoff_guide(results, ctx)
            if args.mode == "revise":
                draft_id = self._pending_draft(results, ctx)
                if draft_id is None:
                    return json.dumps({"status": "refused", "summary": "no draft is pending"})
                extra["draft_id"] = draft_id
            brief = IncidentDeskBrief(
                mode=args.mode,
                issue=args.issue,
                user_words=args.user_words,
                finding_id=finding_id,
                revision_request=args.revision_request,
                acknowledge_possible_duplicates=args.manager_accepted_possible_duplicate,
            )
            return await call("incident_desk", brief, extra)

        async def submit_answer(**values: Any) -> str:
            args = parse(SubmitAnswer, values)
            if isinstance(args, str):
                return args
            if state.captured is None:
                state.captured = args.model_dump()
            return "Answer received. Reply DONE."

        tools = [
            make_tool(
                "ask_store_insight",
                "Diagnose what is wrong in the store and why (devices, network, incidents).",
                AskStoreInsight,
                ask_store_insight,
            ),
            make_tool(
                "ask_kb_guide",
                "Start an approved step-by-step fix (mode start) or continue the guide in "
                "progress with the user's answer (mode continue).",
                AskKbGuide,
                ask_kb_guide,
            ),
            make_tool(
                "ask_incident_desk",
                "Look up incidents (lookup), prepare an incident draft (draft, store "
                "manager only) or change the pending draft (revise).",
                AskIncidentDesk,
                ask_incident_desk,
            ),
            make_tool(
                "submit_answer",
                "Call exactly once at the end with the answer for the user.",
                SubmitAnswer,
                submit_answer,
            ),
        ]
        instructions = PROMPT.read_text(encoding="utf-8")
        reset = CURRENT_RUN.set(state)
        try:
            client = self.client_for(target)
            agent = af.Agent(client, instructions, name="orchestrator", tools=tools)
            try:
                response = await asyncio.wait_for(
                    agent.run(message), settings.agent_turn_timeout_seconds
                )
                outcome.text = (getattr(response, "text", None) or "").strip()
            except TimeoutError:
                outcome.warnings.append("the turn took too long")
        finally:
            CURRENT_RUN.reset(reset)
            outcome.model_calls += state.model_calls
        if state.stop_reason == "disabled":
            outcome.warnings.append("the assistant is switched off")
        elif state.stop_reason == "model_call_limit":
            outcome.warnings.append("orchestrator model call limit reached")
        outcome.answer = state.captured


def diagnosis_of(result: SpecialistResult) -> dict[str, Any]:
    causes = result.body.get("causes") or []
    best = next((c for c in causes if c["staff_can_fix"] and c["kb_tags"]), None)
    best = best or (causes[0] if causes else None)
    cause = None
    if best is not None:
        cause = {
            "finding_id": best.get("finding_id"),
            "node_id": best["root_node"]["id"],
            "element_type": best["root_node"]["element_type"],
            "fault_code": best.get("fault_code"),
            "kb_tags": best.get("kb_tags") or [],
            "staff_can_fix": best.get("staff_can_fix"),
        }
    return {
        "summary": result.summary,
        "outcome": result.outcome,
        "finding_ids": [c["finding_id"] for c in causes if c.get("finding_id")],
        "cause": cause,
    }
