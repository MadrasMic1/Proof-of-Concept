# app/agentic/llm/transport.py
"""Every agent model request goes through MeteredTransport (the HTTP layer of the
OpenAI client that Microsoft Agent Framework drives), so it works the same whatever
the installed Agent Framework version does. Per chat-completions request:

    1. result already submitted  -> a local "done" answer, no request (ends the loop)
    2. AGENT_ENABLED=false       -> a local answer, recorded as refused
    3. model-call cap reached    -> a local answer, recorded as refused
    4. add the request fields the provider / model needs (thinking off for Nemotron,
       temperature, output-token limit) when the request does not set them
    5. send, read usage and latency, write one agent_llm_calls row

The current run is a context variable (RunState), so one client serves every run.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

try:  # the HTTP library the installed openai package uses (as tests/test_llm_providers.py)
    import httpx2 as hx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    import httpx as hx

logger = logging.getLogger("ric.agents")
CHAT_PATH = "/chat/completions"
DROP_HEADERS = frozenset({"content-length", "content-encoding", "transfer-encoding"})


@dataclass
class LlmCallRecord:
    status: str  # succeeded | failed | refused
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    error_code: str | None = None


@dataclass
class RunState:
    """One specialist (or orchestrator) run."""

    agent: str
    profile: str
    correlation_id: uuid.UUID
    max_model_calls: int
    store_id: uuid.UUID | None = None
    chat_session_id: uuid.UUID | None = None
    user_id: str | None = None
    fallback_used: bool = False
    model_calls: int = 0
    captured: dict[str, Any] | None = None
    stop_reason: str | None = None
    calls: list[LlmCallRecord] = field(default_factory=list)

    @property
    def prompt_tokens(self) -> int:
        return sum(c.prompt_tokens or 0 for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c.completion_tokens or 0 for c in self.calls)


CURRENT_RUN: ContextVar[RunState | None] = ContextVar("ric_agent_run", default=None)
Recorder = Callable[[RunState, LlmCallRecord], Awaitable[None]]


def local_answer(text: str, model: str) -> bytes:
    body = {
        "id": f"local-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return json.dumps(body).encode("utf-8")


class MeteredTransport(hx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        extras: dict[str, Any],
        temperature: float,
        max_tokens: int,
        token_param: str,
        enabled: Callable[[], bool],
        recorder: Recorder | None = None,
        inner: hx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.extras = extras
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.token_param = token_param
        self.enabled = enabled
        self.recorder = recorder
        self.inner = inner or hx.AsyncHTTPTransport()

    async def _record(self, state: RunState | None, record: LlmCallRecord) -> None:
        if state is None:
            return
        state.calls.append(record)
        if self.recorder is not None:
            try:
                await self.recorder(state, record)
            except Exception:
                logger.exception("could not record the llm call")

    def _local(self, request: hx.Request, text: str) -> hx.Response:
        return hx.Response(
            200,
            headers={"content-type": "application/json"},
            content=local_answer(text, self.model),
            request=request,
        )

    def _rewrite(self, request: hx.Request) -> hx.Request:
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            return request
        for key, value in self.extras.items():
            body.setdefault(key, value)
        body.setdefault("temperature", self.temperature)
        if "max_tokens" not in body and "max_completion_tokens" not in body:
            body[self.token_param] = self.max_tokens
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in DROP_HEADERS]
        return hx.Request(
            request.method,
            request.url,
            headers=headers,
            content=json.dumps(body).encode("utf-8"),
            extensions=request.extensions,
        )

    async def handle_async_request(self, request: hx.Request) -> hx.Response:
        if request.method != "POST" or not request.url.path.endswith(CHAT_PATH):
            return await self.inner.handle_async_request(request)
        state = CURRENT_RUN.get()
        if state is not None and state.captured is not None:
            return self._local(request, "DONE")
        if not self.enabled():
            if state is not None:
                state.stop_reason = "disabled"
            await self._record(
                state, LlmCallRecord("refused", self.provider, self.model, error_code="disabled")
            )
            return self._local(request, "The assistant is switched off.")
        if state is not None and state.model_calls >= state.max_model_calls:
            state.stop_reason = "model_call_limit"
            await self._record(
                state,
                LlmCallRecord("refused", self.provider, self.model, error_code="model_call_limit"),
            )
            return self._local(request, "Model call limit reached.")
        if state is not None:
            state.model_calls += 1
        started = time.perf_counter()
        try:
            response = await self.inner.handle_async_request(self._rewrite(request))
            content = await response.aread()
        except Exception as exc:
            await self._record(
                state,
                LlmCallRecord(
                    "failed",
                    self.provider,
                    self.model,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    error_code=exc.__class__.__name__[:60],
                ),
            )
            raise
        latency = round((time.perf_counter() - started) * 1000)
        usage: dict[str, Any] = {}
        model = self.model
        if response.status_code == 200:
            try:
                parsed = json.loads(content)
                usage = parsed.get("usage") or {}
                model = parsed.get("model") or model
            except ValueError:
                pass
        await self._record(
            state,
            LlmCallRecord(
                "succeeded" if response.status_code == 200 else "failed",
                self.provider,
                str(model)[:120],
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                latency,
                None if response.status_code == 200 else f"HTTP {response.status_code}",
            ),
        )
        headers = [(k, v) for k, v in response.headers.items() if k.lower() not in DROP_HEADERS]
        return hx.Response(response.status_code, headers=headers, content=content, request=request)

    async def aclose(self) -> None:
        await self.inner.aclose()
