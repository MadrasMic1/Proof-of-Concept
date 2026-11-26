# app/agentic/agents/runtime.py
"""Run one specialist on one task brief.

    1. mint a User Context Token for this agent and run (Phase 5 tokens)
    2. connect to the MCP server (FastMCP client, Bearer token) and build the agent's
       tools from the policy (tools_for(agent, role)) plus submit_result
    3. run the Agent Framework agent (instructions = the versioned prompt, the brief
       as one JSON message, no history) under AGENT_SPECIALIST_TIMEOUT_SECONDS with the
       tool-call cap (bridge) and the model-call cap (transport)
    4. validate the report against this run's tool results (specialists.py); on
       problems (or no report) run ONCE more with the problems added to the brief
    5. if the provider is unavailable after its retries, run again on the fallback
       target (AGENT_FALLBACK_*), when one is configured
Specialists are stateless: continuity comes from the brief and the guide / draft state.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import agent_framework as af
import openai
from pydantic import BaseModel, ValidationError

from app.agentic.agents.bridge import McpBridge, ToolCallRecord, submit_tool
from app.agentic.agents.schemas import RunTrace, SpecialistResult, TrustedContext
from app.agentic.agents.specialists import SPECIALISTS, SpecialistSpec
from app.agentic.common.tokens import mint_token
from app.agentic.config import AgenticSettings
from app.agentic.llm.client import ModelTarget
from app.agentic.llm.transport import CURRENT_RUN, RunState
from app.agentic.tools.policy import ToolPolicy

ChatClientFactory = Callable[[ModelTarget], Any]
UNAVAILABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass
class AgentDeps:
    settings: AgenticSettings
    policy: ToolPolicy
    mcp_url: str
    targets: list[ModelTarget]  # primary first, then the fallback (if any)
    chat_client: ChatClientFactory  # target -> chat client (cached by the caller)


def is_unavailable(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, openai.APIConnectionError | openai.APITimeoutError):
            return True
        if isinstance(current, openai.APIStatusError):
            return current.status_code in UNAVAILABLE_STATUS or current.status_code >= 500
        current = current.__cause__ or current.__context__
    return False


def _message(
    spec: SpecialistSpec, brief: BaseModel, trusted: TrustedContext, notes: list[str]
) -> str:
    task: dict[str, Any] = {"task": brief.model_dump(mode="json", exclude_none=True)}
    if trusted.guide_id is not None and spec.name in ("kb_guide", "incident_desk"):
        task["guide_id"] = str(trusted.guide_id)
    if trusted.draft_id is not None and spec.name == "incident_desk":
        task["draft_id"] = str(trusted.draft_id)
    task["user_role"] = trusted.role
    if notes:
        task["previous_attempt_rejected"] = notes
    return json.dumps(task, ensure_ascii=False)


async def _attempt(
    spec: SpecialistSpec,
    brief: BaseModel,
    trusted: TrustedContext,
    deps: AgentDeps,
    target: ModelTarget,
    notes: list[str],
) -> tuple[RunState, list[ToolCallRecord]]:
    settings = deps.settings
    state = RunState(
        agent=spec.name,
        profile="specialist",
        correlation_id=trusted.correlation_id,
        max_model_calls=settings.agent_specialist_max_model_calls,
        store_id=trusted.store_id,
        chat_session_id=trusted.chat_session_id,
        user_id=trusted.user_id,
        fallback_used=target.fallback,
    )
    token = mint_token(
        settings,
        user_id=trusted.user_id,
        role=trusted.role,
        store_id=trusted.store_id,
        agent=spec.name,
        correlation_id=trusted.correlation_id,
        policy_version=deps.policy.version_text,
        chat_session_id=trusted.chat_session_id,
    )
    allowed = deps.policy.tools_for(spec.name, trusted.role)

    def capture(values: dict[str, Any]) -> None:
        if state.captured is None:
            state.captured = values

    reset = CURRENT_RUN.set(state)
    try:
        async with McpBridge(deps.mcp_url, token, spec.max_tool_calls(settings)) as bridge:
            tools = [*bridge.function_tools(allowed), submit_tool(spec.report_model, capture)]
            agent = af.Agent(
                deps.chat_client(target),
                spec.instructions(),
                name=spec.name,
                tools=tools,
            )
            try:
                await asyncio.wait_for(
                    agent.run(_message(spec, brief, trusted, notes)),
                    settings.agent_specialist_timeout_seconds,
                )
            except TimeoutError:
                state.stop_reason = "timeout"
            return state, list(bridge.records)
    finally:
        CURRENT_RUN.reset(reset)


async def run_specialist(
    agent: str, brief: BaseModel, trusted: TrustedContext, deps: AgentDeps
) -> SpecialistResult:
    spec = SPECIALISTS[agent]
    if not isinstance(brief, spec.brief_model):
        raise TypeError(f"{agent} needs a {spec.brief_model.__name__}")
    started = time.perf_counter()
    notes: list[str] = []
    trace = RunTrace()
    tool_calls: list[Any] = []
    model_calls = prompt_tokens = completion_tokens = attempts = 0
    targets = list(deps.targets)
    target = targets.pop(0)
    last_problems: list[str] = []
    while True:
        attempts += 1
        try:
            state, records = await _attempt(spec, brief, trusted, deps, target, notes)
        except Exception as exc:
            if is_unavailable(exc) and targets:
                target = targets.pop(0)
                continue
            raise
        tool_calls += [r.trace() for r in records]
        model_calls += state.model_calls
        prompt_tokens += state.prompt_tokens
        completion_tokens += state.completion_tokens
        trace = RunTrace(
            tool_calls=tool_calls,
            model_calls=model_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            duration_ms=round((time.perf_counter() - started) * 1000),
            attempts=attempts,
            fallback_used=target.fallback,
            model=target.label,
            stop_reason=state.stop_reason,
        )
        if state.stop_reason == "disabled":
            return SpecialistResult(
                agent=agent, status="error", outcome="disabled",
                summary="The assistant is switched off.", trace=trace,
            )  # fmt: skip
        problems: list[str]
        if state.captured is None:
            problems = ["no result: finish by calling submit_result exactly once"]
        else:
            try:
                report = spec.report_model.model_validate(state.captured)
            except ValidationError as exc:
                problems = [f"submit_result: {e['msg']} at {e['loc']}" for e in exc.errors()][:5]
            else:
                outcome, summary, suggested, body, problems = spec.finalize(
                    report, records, trusted, brief, deps.settings
                )
                if not problems:
                    return SpecialistResult(
                        agent=agent,
                        status="ok",
                        outcome=outcome,
                        summary=summary,
                        suggested_next=suggested,  # type: ignore[arg-type]
                        body=body,
                        trace=trace,
                    )
        last_problems = problems
        if attempts > deps.settings.store_insight_validator_repair_attempts:
            break
        notes = problems
    return SpecialistResult(
        agent=agent,
        status="error",
        outcome="invalid_result",
        summary="The specialist could not give a result supported by the data.",
        problems=last_problems,
        trace=trace,
    )
