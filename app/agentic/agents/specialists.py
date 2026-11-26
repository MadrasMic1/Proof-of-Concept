# app/agentic/agents/specialists.py
"""The three specialists: prompt, brief, report model, and the code that turns the
model's report plus this run's tool results into a validated result.

Each finalize() returns (outcome, summary, suggested_next, body, problems). Problems
mean the report is not supported by the tool results: the runtime asks the model to
repair once, then gives up (status error). Fields the model must not decide
(staff_can_fix, kb_tags, step text, draft fields, ids, suggested_next) are built here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.agentic.agents import evidence
from app.agentic.agents.bridge import ToolCallRecord
from app.agentic.agents.schemas import (
    IncidentDeskBrief,
    IncidentDeskReport,
    KbGuideBrief,
    KbGuideReport,
    StoreInsightBrief,
    StoreInsightReport,
    TrustedContext,
)
from app.agentic.config import AgenticSettings

PROMPTS = Path(__file__).resolve().parent / "prompts"
Finalized = tuple[str, str, str, dict[str, Any], list[str]]


@dataclass(frozen=True)
class SpecialistSpec:
    name: str
    prompt_file: str
    brief_model: type[BaseModel]
    report_model: type[BaseModel]
    max_tool_calls: Callable[[AgenticSettings], int]
    finalize: Callable[
        [BaseModel, Sequence[ToolCallRecord], TrustedContext, BaseModel, AgenticSettings],
        Finalized,
    ]

    def instructions(self) -> str:
        return (PROMPTS / self.prompt_file).read_text(encoding="utf-8")


# --- Store Insight -------------------------------------------------------------------


def _cause(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": finding["finding_id"],
        "certainty": "suspected" if finding.get("kind") == "suspect" else "confirmed",
        "root_node": finding["root_node"],
        "status": finding["status"],
        "fault_code": finding.get("fault_code"),
        "severity": finding["severity"],
        "impacted_nodes": finding.get("impacted_nodes") or [],
        "evidence_path": finding.get("evidence_path") or [],
        "explanation": finding["impact_text"],
        "staff_can_fix": bool(finding.get("staff_can_fix")),
        "kb_tags": finding.get("kb_tags") or [],
        "covered_by_incident": finding.get("covered_by_incident"),
        "in_maintenance": bool(finding.get("in_maintenance")),
    }


def finalize_store_insight(
    report: BaseModel,
    records: Sequence[ToolCallRecord],
    trusted: TrustedContext,
    brief: BaseModel,
    settings: AgenticSettings,
) -> Finalized:
    assert isinstance(report, StoreInsightReport)
    findings = evidence.findings_seen(records)
    nodes = evidence.nodes_seen(records)
    problems = [
        f"finding {fid} was not returned by store_get_findings in this task"
        for fid in report.finding_ids
        if fid not in findings
    ]
    for label, ids in (
        ("subject", report.subject_node_ids),
        ("suspected", report.suspected_node_ids),
        ("device option", report.device_option_ids),
    ):
        problems += [
            f"{label} node {nid} was not in any tool result" for nid in ids if nid not in nodes
        ]
    outcome = report.outcome
    if outcome in ("cause_found", "covered_by_incident") and not (
        report.finding_ids or report.suspected_node_ids
    ):
        problems.append(f"outcome {outcome} needs finding_ids or suspected_node_ids")
    if outcome == "ambiguous_device" and len(report.device_option_ids) < 2:
        problems.append("ambiguous_device needs at least two device_option_ids")
    if problems:
        return outcome, report.summary, "none", {}, problems

    causes = [_cause(findings[fid]) for fid in report.finding_ids]
    causes += [
        {
            "finding_id": None,
            "certainty": "suspected",
            "root_node": nodes[nid],
            "status": "unknown",
            "fault_code": None,
            "severity": "minor",
            "impacted_nodes": [],
            "evidence_path": [],
            "explanation": "suspected from the store's dependency graph and history",
            "staff_can_fix": False,
            "kb_tags": [],
            "covered_by_incident": None,
            "in_maintenance": False,
        }
        for nid in report.suspected_node_ids
    ]
    causes = causes[: settings.store_insight_max_causes]
    if causes and all(c["covered_by_incident"] for c in causes):
        outcome = "covered_by_incident"
    elif outcome == "covered_by_incident":
        outcome = "cause_found"
    fixable = any(c["staff_can_fix"] and c["kb_tags"] for c in causes)
    suggested = {
        "cause_found": "guide" if fixable else "offer_ticket",
        "covered_by_incident": "check_incident",
        "planned_maintenance": "none",
        "no_fault_found": "offer_ticket" if evidence.flapping_seen(records) else "none",
        "status_unknown": "offer_ticket",
        "ambiguous_device": "ask_device",
    }[outcome]
    body = {
        "outcome": outcome,
        "subject_nodes": [nodes[n] for n in report.subject_node_ids],
        "causes": causes,
        "device_options": [nodes[n] for n in report.device_option_ids],
    }
    return outcome, report.summary, suggested, body, []


# --- KB Guide --------------------------------------------------------------------------

GUIDE_TOOLS = ("kb_guide_start", "kb_guide_get_state", "kb_guide_record_outcome", "kb_guide_end")


def finalize_kb_guide(
    report: BaseModel,
    records: Sequence[ToolCallRecord],
    trusted: TrustedContext,
    brief: BaseModel,
    settings: AgenticSettings,
) -> Finalized:
    assert isinstance(report, KbGuideReport) and isinstance(brief, KbGuideBrief)
    problems: list[str] = []
    if brief.mode == "continue" and trusted.guide_id is not None:
        expected = str(trusted.guide_id)
        for record in records:
            used = record.arguments.get("guide_id")
            if record.tool in GUIDE_TOOLS and used is not None and str(used) != expected:
                problems.append(
                    f"{record.tool} used guide {used}, the guide in progress is {expected}"
                )
    last = evidence.last_data(records, *GUIDE_TOOLS)
    guide = last[1] if last else None
    outcome = report.outcome
    if guide is not None:
        status = guide["status"]
        derived = "needs_user_input" if status == "in_progress" else status
        if derived == "abandoned":
            derived = "exhausted"
        if outcome != "problem_changed":
            outcome = derived
    elif outcome in ("needs_user_input", "resolved", "exhausted", "needs_technician"):
        problems.append(f"outcome {outcome} needs a guide started or read in this task")
    candidates: list[dict[str, Any]] = []
    found = evidence.last_data(records, "kb_find_articles")
    offered = {c["kb_ref"]: c for c in (found[1]["candidates"] if found else [])}
    for ref in report.candidate_kb_refs:
        if ref not in offered:
            problems.append(f"article {ref} was not returned by kb_find_articles")
        else:
            candidates.append(offered[ref])
    if outcome == "choose_article" and len(candidates) < 2:
        problems.append("choose_article needs at least two candidate_kb_refs")
    if problems:
        return outcome, report.summary, "none", {}, problems

    step = None
    if guide is not None and guide.get("step"):
        raw = guide["step"]
        step = {
            "guide_id": guide["guide_id"],
            "kb_ref": guide["kb_ref"],
            "kb_version": guide["kb_version"],
            "title": guide["title"],
            "step_id": raw["step_id"],
            "step_no": raw["step_no"],
            "instruction": raw["instruction"],
            "ask": raw["ask"],
            "options": raw["options"],
            "state_version": guide["state_version"],
        }
    suggested = {
        "needs_user_input": "none",
        "resolved": "none",
        "exhausted": "offer_ticket",
        "needs_technician": "offer_ticket",
        "no_article": "offer_ticket",
        "choose_article": "none",
        "problem_changed": "rediagnose",
    }[outcome]
    body = {
        "outcome": outcome,
        "guide_id": guide["guide_id"] if guide else None,
        "step": step,
        "candidates": candidates,
        "steps_tried": guide.get("steps_tried", []) if guide else [],
    }
    return outcome, report.summary, suggested, body, []


# --- Incident Desk ----------------------------------------------------------------------

DRAFT_TOOLS = ("itsm_prepare_draft", "itsm_revise_draft", "itsm_get_draft")


def finalize_incident_desk(
    report: BaseModel,
    records: Sequence[ToolCallRecord],
    trusted: TrustedContext,
    brief: BaseModel,
    settings: AgenticSettings,
) -> Finalized:
    assert isinstance(report, IncidentDeskReport) and isinstance(brief, IncidentDeskBrief)
    incidents = evidence.incidents_seen(records)
    problems = [
        f"incident {n} was not returned by an incident tool in this task"
        for n in report.incident_numbers
        if n not in incidents
    ]
    if brief.mode == "revise" and trusted.draft_id is not None:
        expected = str(trusted.draft_id)
        for record in records:
            used = record.arguments.get("draft_id")
            if record.tool in DRAFT_TOOLS and used is not None and str(used) != expected:
                problems.append(f"{record.tool} used draft {used}, the pending draft is {expected}")
    last = evidence.last_data(records, *DRAFT_TOOLS)
    outcome = report.outcome
    draft = None
    duplicates = None
    if last is not None:
        tool, data = last
        draft = data.get("draft")
        if tool == "itsm_prepare_draft":
            outcome = data["outcome"]
            duplicates = data.get("duplicates")
        elif tool == "itsm_revise_draft":
            outcome = "draft_revised"
        if report.draft_id and draft and report.draft_id != draft.get("draft_id"):
            problems.append(f"draft {report.draft_id} is not the draft in the tool results")
    elif outcome in ("draft_ready", "draft_revised", "duplicate_found", "possible_duplicate"):
        problems.append(f"outcome {outcome} needs itsm_prepare_draft or itsm_revise_draft")
    if outcome == "incidents_found" and not report.incident_numbers:
        problems.append("incidents_found needs incident_numbers")
    if problems:
        return outcome, report.summary, "none", {}, problems
    shown = [incidents[n] for n in report.incident_numbers]
    if duplicates and not shown:
        shown = [m for m in duplicates.get("matches", []) if m.get("incident_number")]
    suggested = {
        "incidents_found": "none",
        "no_incidents": "none",
        "duplicate_found": "check_incident",
        "possible_duplicate": "none",
        "draft_ready": "show_draft",
        "draft_revised": "show_draft",
        "needs_rediagnosis": "rediagnose",
    }[outcome]
    body = {"outcome": outcome, "incidents": shown, "draft": draft, "duplicates": duplicates}
    return outcome, report.summary, suggested, body, []


SPECIALISTS: dict[str, SpecialistSpec] = {
    "store_insight": SpecialistSpec(
        "store_insight",
        "store_insight.v1.md",
        StoreInsightBrief,
        StoreInsightReport,
        lambda s: s.store_insight_max_tool_calls,
        finalize_store_insight,
    ),
    "kb_guide": SpecialistSpec(
        "kb_guide",
        "kb_guide.v1.md",
        KbGuideBrief,
        KbGuideReport,
        lambda s: s.kb_guide_max_tool_calls,
        finalize_kb_guide,
    ),
    "incident_desk": SpecialistSpec(
        "incident_desk",
        "incident_desk.v1.md",
        IncidentDeskBrief,
        IncidentDeskReport,
        lambda s: s.incident_desk_max_tool_calls,
        finalize_incident_desk,
    ),
}
