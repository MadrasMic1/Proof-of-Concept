# tests/agentic/test_agent_llm.py
"""Agentic Phase 6: the metered transport, model targets, the bridge helpers and the
three finalizers (no database, no network)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import openai
import pytest
from pydantic import SecretStr

from app.agentic.agents.bridge import ToolCallRecord, for_model, make_tool, submit_tool
from app.agentic.agents.runtime import is_unavailable
from app.agentic.agents.schemas import (
    IncidentDeskBrief,
    IncidentDeskReport,
    KbGuideBrief,
    KbGuideReport,
    StoreInsightBrief,
    StoreInsightReport,
    TrustedContext,
)
from app.agentic.agents.specialists import (
    finalize_incident_desk,
    finalize_kb_guide,
    finalize_store_insight,
)
from app.agentic.config import AgenticSettings
from app.agentic.llm.client import build_chat_client, fallback_target, primary_target
from app.agentic.llm.transport import CURRENT_RUN, MeteredTransport, RunState, hx
from app.agentic.tools.store import FindingsArgs
from app.llm.types import LlmConfigError

SETTINGS = AgenticSettings(_env_file=None, environment="test")
TRUSTED = TrustedContext("u", "store_manager", uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
NODE = {"kind": "device", "id": str(uuid.uuid4()), "name": "RF Gun 02", "element_type": "rf_gun",
        "label": "RF gun RF Gun 02"}  # fmt: skip
FINDING = {
    "finding_id": "F-1a2b3c4d",
    "kind": "root_cause",
    "severity": "minor",
    "root_node": NODE,
    "status": "degraded",
    "fault_code": "BATTERY_LOW",
    "impacted_nodes": [],
    "evidence_path": [],
    "impact_text": "RF gun RF Gun 02 works intermittently.",
    "staff_can_fix": True,
    "kb_tags": ["rf-battery"],
    "covered_by_incident": None,
    "in_maintenance": False,
}


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "data": data, "as_of": "2026-10-13T03:30:00Z", "untrusted_fields": [],
            "error": None}  # fmt: skip


def _completion(request: Any) -> Any:
    return hx.Response(
        200,
        json={
            "id": "x",
            "object": "chat.completion",
            "created": 0,
            "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
        },
    )  # fmt: skip


def _transport(enabled: bool = True, seen: list[Any] | None = None) -> MeteredTransport:
    def handler(request: Any) -> Any:
        if seen is not None:
            seen.append(json.loads(request.content))
        return _completion(request)

    return MeteredTransport(
        provider="nvidia",
        model="nvidia/nemotron-3.5-lightning-30b-a3b",
        extras={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.0,
        max_tokens=512,
        token_param="max_tokens",
        enabled=lambda: enabled,
        inner=hx.MockTransport(handler),
    )


async def _post(transport: MeteredTransport, body: dict[str, Any]) -> dict[str, Any]:
    async with hx.AsyncClient(transport=transport) as client:
        response = await client.post("https://x.test/v1/chat/completions", json=body)
    return response.json()


def _state(**kw: Any) -> RunState:
    return RunState(agent="store_insight", profile="specialist", correlation_id=uuid.uuid4(),
                    max_model_calls=kw.pop("max_model_calls", 3), **kw)  # fmt: skip


async def test_transport_adds_fields_and_meters() -> None:
    seen: list[Any] = []
    state = _state()
    token = CURRENT_RUN.set(state)
    try:
        body = {"model": "m", "messages": [], "temperature": 0.7}
        answer = await _post(_transport(seen=seen), body)
    finally:
        CURRENT_RUN.reset(token)
    assert answer["choices"][0]["message"]["content"] == "hi"
    sent = seen[0]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["temperature"] == 0.7 and sent["max_tokens"] == 512
    assert state.model_calls == 1
    [call] = state.calls
    assert (call.status, call.prompt_tokens, call.completion_tokens) == ("succeeded", 50, 5)


async def test_transport_switch_cap_and_captured() -> None:
    seen: list[Any] = []
    state = _state(max_model_calls=1)
    token = CURRENT_RUN.set(state)
    try:
        off = await _post(_transport(enabled=False, seen=seen), {"messages": []})
        assert "switched off" in off["choices"][0]["message"]["content"]
        assert state.stop_reason == "disabled" and state.calls[-1].status == "refused"
        transport = _transport(seen=seen)
        await _post(transport, {"messages": []})
        capped = await _post(transport, {"messages": []})
        assert "limit" in capped["choices"][0]["message"]["content"]
        assert state.stop_reason == "model_call_limit"
        state.captured = {"outcome": "x"}
        done = await _post(transport, {"messages": []})
        assert done["choices"][0]["message"]["content"] == "DONE"
    finally:
        CURRENT_RUN.reset(token)
    assert len(seen) == 1  # only one request reached the model


def test_targets_reuse_the_worker_key() -> None:
    core = SimpleNamespace(llm_provider="nvidia", llm_api_key=SecretStr("nvapi-x"))
    settings = SETTINGS.model_copy(update={"agent_llm_provider": "nvidia"})
    target = primary_target(settings, core, "specialist")  # type: ignore[arg-type]
    assert (target.model, target.api_key) == ("nvidia/nemotron-3.5-lightning-30b-a3b", "nvapi-x")
    assert target.base_url == "https://integrate.api.nvidia.com/v1"
    assert fallback_target(settings, core) is None  # type: ignore[arg-type]
    groq = SimpleNamespace(llm_provider="groq", llm_api_key=SecretStr("g"))
    with pytest.raises(LlmConfigError, match="AGENT_LLM_API_KEY"):
        primary_target(settings, groq, "specialist")  # type: ignore[arg-type]
    fake = primary_target(SETTINGS, core, "specialist")  # type: ignore[arg-type]
    with pytest.raises(LlmConfigError, match="scripted model"):
        build_chat_client(fake, SETTINGS, "specialist")


def test_unavailable_detection() -> None:
    request = hx.Request("POST", "https://x.test")
    busy = openai.RateLimitError("busy", response=hx.Response(429, request=request), body=None)
    bad = openai.BadRequestError("bad", response=hx.Response(400, request=request), body=None)
    wrapped = RuntimeError("framework error")
    wrapped.__cause__ = busy
    assert is_unavailable(busy) and is_unavailable(wrapped)
    assert not is_unavailable(bad) and not is_unavailable(ValueError("x"))


def test_bridge_helpers() -> None:
    envelope = {**_ok({"step": {"instruction": "Open the cover", "ask": "Done?"}})}
    envelope["untrusted_fields"] = ["data.step.instruction"]
    shown = json.loads(for_model(envelope))
    assert shown["data"]["step"]["instruction"] == (
        '<untrusted source="ric-data">Open the cover</untrusted>'
    )
    assert shown["data"]["step"]["ask"] == "Done?"

    async def noop(**values: Any) -> str:
        return "x"

    tool = make_tool("store_get_findings", "Use when: test.", FindingsArgs, noop)
    assert getattr(tool, "name", None) == "store_get_findings"
    captured: list[dict[str, Any]] = []
    submit = submit_tool(StoreInsightReport, captured.append)
    assert getattr(submit, "name", None) == "submit_result"


def _records(*items: tuple[str, dict[str, Any], dict[str, Any]]) -> list[ToolCallRecord]:
    return [ToolCallRecord(tool, args, env) for tool, args, env in items]


def test_finalize_store_insight() -> None:
    records = _records(("store_get_findings", {}, _ok({"findings": [FINDING]})))
    brief = StoreInsightBrief(question="scanner dies")
    report = StoreInsightReport(
        outcome="cause_found",
        summary="Battery low.",
        finding_ids=["F-1a2b3c4d"],
        subject_node_ids=[NODE["id"]],
    )
    outcome, _, suggested, body, problems = finalize_store_insight(
        report, records, TRUSTED, brief, SETTINGS
    )
    assert problems == [] and (outcome, suggested) == ("cause_found", "guide")
    cause = body["causes"][0]
    assert cause["staff_can_fix"] and cause["kb_tags"] == ["rf-battery"]
    covered = {**FINDING, "covered_by_incident": "INC00001304", "staff_can_fix": False}
    records = _records(("store_get_findings", {}, _ok({"findings": [covered]})))
    outcome, _, suggested, _, _ = finalize_store_insight(report, records, TRUSTED, brief, SETTINGS)
    assert (outcome, suggested) == ("covered_by_incident", "check_incident")
    invented = StoreInsightReport(outcome="cause_found", summary="x", finding_ids=["F-deadbeef"],
                                  suspected_node_ids=[str(uuid.uuid4())])  # fmt: skip
    *_, problems = finalize_store_insight(invented, records, TRUSTED, brief, SETTINGS)
    assert len(problems) == 2


def test_finalize_kb_guide() -> None:
    step = {
        "step_id": "S2",
        "step_no": 2,
        "instruction": "Pull the paper out.",
        "ask": "Does it print?",
        "options": [{"key": "works", "label": "Yes"}],
    }
    guide = {
        "guide_id": str(uuid.uuid4()),
        "status": "in_progress",
        "kb_ref": "KB-PRN-001",
        "kb_version": 1,
        "title": "Printer paper jam",
        "state_version": 2,
        "steps_tried": [],
        "step": step,
    }
    trusted = TrustedContext("u", "store_staff", uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                             guide_id=uuid.UUID(guide["guide_id"]))  # fmt: skip
    records = _records(("kb_guide_record_outcome", {"guide_id": guide["guide_id"]}, _ok(guide)))
    brief = KbGuideBrief(mode="continue", user_report="paper was stuck")
    report = KbGuideReport(outcome="resolved", summary="Next step.", guide_id=guide["guide_id"])
    outcome, _, suggested, body, problems = finalize_kb_guide(
        report, records, trusted, brief, SETTINGS
    )
    assert problems == [] and (outcome, suggested) == ("needs_user_input", "none")
    assert body["step"]["instruction"] == "Pull the paper out."
    other = _records(("kb_guide_get_state", {"guide_id": str(uuid.uuid4())}, _ok(guide)))
    *_, problems = finalize_kb_guide(report, other, trusted, brief, SETTINGS)
    assert "the guide in progress is" in problems[0]
    none = KbGuideReport(outcome="no_article", summary="Nothing.")
    outcome, _, suggested, _, problems = finalize_kb_guide(
        none, [], trusted, KbGuideBrief(mode="start", issue="x"), SETTINGS
    )
    assert problems == [] and suggested == "offer_ticket"


def test_finalize_incident_desk() -> None:
    draft = {"draft_id": str(uuid.uuid4()), "version": 1, "status": "pending"}
    prepared = _ok({"outcome": "draft_ready", "draft": draft,
                    "duplicates": {"result": "none", "matches": []}})  # fmt: skip
    records = _records(("itsm_prepare_draft", {}, prepared))
    brief = IncidentDeskBrief(mode="draft", issue="battery")
    report = IncidentDeskReport(outcome="incidents_found", summary="Draft ready.",
                                draft_id=draft["draft_id"])  # fmt: skip
    outcome, _, suggested, body, problems = finalize_incident_desk(
        report, records, TRUSTED, brief, SETTINGS
    )
    assert problems == [] and (outcome, suggested) == ("draft_ready", "show_draft")
    assert body["draft"]["draft_id"] == draft["draft_id"]
    lookup = IncidentDeskReport(outcome="incidents_found", summary="x",
                                incident_numbers=["INC00009999"])  # fmt: skip
    *_, problems = finalize_incident_desk(
        lookup, [], TRUSTED, IncidentDeskBrief(mode="lookup", issue="x"), SETTINGS
    )
    assert "INC00009999" in problems[0]
