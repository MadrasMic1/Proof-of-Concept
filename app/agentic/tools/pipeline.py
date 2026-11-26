# app/agentic/tools/pipeline.py
"""The call pipeline every MCP tool call goes through, in this order:

    1. authenticate  the User Context Token                       -> invalid_token
    2. switch        AGENT_ENABLED                                 -> disabled
    3. authorise     policy version, agent's tools, role denials   -> policy_mismatch / forbidden
    4. validate      the tool's argument model                     -> invalid_input
    5. limits        calls per chat per minute, identical calls    -> rate_limited
    6. run           own transaction on the MCP engine (ric_mcp), MCP_TOOL_TIMEOUT_SECONDS;
                     read tools: read-only REPEATABLE READ, rolled back; write tools commit
    7. shape         envelope; untrusted text capped at MCP_MAX_TEXT_FIELD_CHARS
    8. audit         one agent_tool_calls row in its own transaction
A tool never raises to MCP: every failure is an error envelope with a fixed code, so
the agent can react (e.g. conflict on a guide move -> read the state again).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.common.envelope import failure, success
from app.agentic.common.tokens import TokenError, ToolIdentity, verify_token
from app.agentic.config import AgenticSettings
from app.agentic.kb.embeddings import Embedder
from app.agentic.store_context.snapshot import begin_read_only
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.tools.base import ToolContext, ToolFailure, ToolOutput
from app.agentic.tools.policy import ToolPolicy
from app.agentic.tools.registry import TOOLS, ToolSpec
from app.agentic.values import ErrorCode
from app.core.clock import Clock
from app.core.errors import NotFoundError
from app.models import AgentToolCall

logger = logging.getLogger("ric.mcp")
TRUNCATED = " [truncated]"
MAX_ERROR_CHARS = 500


def args_hash(raw: dict[str, Any]) -> str:
    text = json.dumps(raw, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def cap_untrusted(data: Any, path: str, limit: int) -> Any:
    """Cap the text at path ("a.b", "items[].text") to limit characters."""
    if not path:
        if isinstance(data, str) and len(data) > limit:
            return data[:limit] + TRUNCATED
        return data
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        key = head[:-2]
        if isinstance(data, dict) and isinstance(data.get(key), list):
            data[key] = [cap_untrusted(item, rest, limit) for item in data[key]]
        return data
    if isinstance(data, dict) and head in data:
        data[head] = cap_untrusted(data[head], rest, limit)
    return data


class RateLimiter:
    """In memory, per MCP process (one process in the POC)."""

    def __init__(self, per_minute: int, identical_max: int) -> None:
        self.per_minute = per_minute
        self.identical_max = identical_max
        self._calls: dict[str, deque[float]] = {}
        self._identical: dict[str, tuple[int, float]] = {}

    def check(self, session_key: str, call_key: str, now: float) -> str | None:
        window = self._calls.setdefault(session_key, deque())
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= self.per_minute:
            return f"more than {self.per_minute} tool calls in a minute"
        count, first = self._identical.get(call_key, (0, now))
        if count >= self.identical_max:
            return f"the same call was already made {count} times in this run"
        window.append(now)
        self._identical[call_key] = (count + 1, first)
        if len(self._identical) > 10_000:
            self._identical = {k: v for k, v in self._identical.items() if now - v[1] < 900}
        return None


@dataclass
class ToolRuntime:
    settings: AgenticSettings
    catalogue: Catalogue
    policy: ToolPolicy
    factory: async_sessionmaker[AsyncSession]
    clock: Clock
    embedder: Embedder | None = None
    tools: dict[str, ToolSpec] = field(default_factory=lambda: dict(TOOLS))
    limiter: RateLimiter | None = None

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = RateLimiter(
                self.settings.mcp_rate_limit_per_session_per_minute,
                self.settings.mcp_max_identical_calls_per_run,
            )

    async def call(self, name: str, token: str | None, raw: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        spec = self.tools.get(name)
        raw = {k: v for k, v in raw.items() if spec is None or k in spec.args_model.model_fields}
        identity: ToolIdentity | None = None
        try:
            identity = verify_token(self.settings, token)
            envelope = await self._authorised(name, spec, identity, raw)
        except TokenError as exc:
            envelope = failure(ErrorCode.INVALID_TOKEN, str(exc)).model_dump(mode="json")
        error = envelope.get("error") or {}
        await self._audit(
            name,
            identity,
            str(error.get("code") or "ok"),
            int((time.monotonic() - started) * 1000),
            args_hash(raw),
            error.get("message"),
        )
        return envelope

    async def _authorised(
        self, name: str, spec: ToolSpec | None, identity: ToolIdentity, raw: dict[str, Any]
    ) -> dict[str, Any]:
        def fail(code: ErrorCode, message: str) -> dict[str, Any]:
            return failure(code, message).model_dump(mode="json")

        if not self.settings.agent_enabled:
            return fail(ErrorCode.DISABLED, "the assistant is switched off")
        if identity.policy_version != self.policy.version_text:
            return fail(
                ErrorCode.POLICY_MISMATCH,
                f"token policy {identity.policy_version!r}, server {self.policy.version_text!r}",
            )
        if spec is None or not self.policy.allows(identity.agent, identity.role, name):
            return fail(ErrorCode.FORBIDDEN, f"{name} is not allowed for this agent and role")
        try:
            args = spec.args_model.model_validate(raw)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'args'}: {e['msg']}" for e in exc.errors()
            )
            return fail(ErrorCode.INVALID_INPUT, problems[:MAX_ERROR_CHARS])
        session_key = str(identity.chat_session_id or identity.user_id)
        call_key = f"{identity.correlation_id}|{identity.agent}|{name}|{args_hash(raw)}"
        assert self.limiter is not None
        limited = self.limiter.check(session_key, call_key, time.monotonic())
        if limited:
            return fail(ErrorCode.RATE_LIMITED, limited)
        return await self._run(spec, identity, args)

    async def _run(self, spec: ToolSpec, identity: ToolIdentity, args: Any) -> dict[str, Any]:
        ctx = ToolContext(
            identity=identity,
            now=self.clock.now(),
            settings=self.settings,
            catalogue=self.catalogue,
            embedder=self.embedder,
        )
        limit = self.settings.mcp_max_text_field_chars
        try:
            async with self.factory() as session:
                try:
                    if not spec.writes:
                        await begin_read_only(session, self.settings.mcp_tool_timeout_seconds)
                    output: ToolOutput = await asyncio.wait_for(
                        spec.function(session, ctx, args), self.settings.mcp_tool_timeout_seconds
                    )
                    if spec.writes:
                        await session.commit()
                    else:
                        await session.rollback()
                except BaseException:
                    await session.rollback()
                    raise
        except ToolFailure as exc:
            return failure(exc.code, exc.message[:MAX_ERROR_CHARS]).model_dump(mode="json")
        except NotFoundError as exc:
            return failure(ErrorCode.NOT_FOUND, str(exc)).model_dump(mode="json")
        except TimeoutError:
            return failure(ErrorCode.TIMEOUT, f"{spec.name} took too long").model_dump(mode="json")
        except DBAPIError as exc:
            text = str(exc).lower()
            if "permission denied" in text:
                code, message = ErrorCode.FORBIDDEN, "the tool is not allowed to do that"
            elif "statement timeout" in text or "canceling statement" in text:
                code, message = ErrorCode.TIMEOUT, f"{spec.name} took too long"
            else:
                code, message = ErrorCode.UNAVAILABLE, "the database is not available"
            logger.warning("tool %s database error: %s", spec.name, exc.__class__.__name__)
            return failure(code, message).model_dump(mode="json")
        except Exception:
            logger.exception("tool %s failed", spec.name)
            return failure(ErrorCode.ERROR, f"{spec.name} failed").model_dump(mode="json")
        data = output.data
        for path in output.untrusted:
            data = cap_untrusted(data, path, limit)
        envelope = success(data, output.as_of, [f"data.{p}" for p in output.untrusted])
        return envelope.model_dump(mode="json")

    async def _audit(
        self,
        name: str,
        identity: ToolIdentity | None,
        outcome: str,
        duration_ms: int,
        hashed: str,
        message: str | None,
    ) -> None:
        row: dict[str, Any] = {
            "correlation_id": identity.correlation_id if identity else uuid.uuid4(),
            "chat_session_id": identity.chat_session_id if identity else None,
            "store_id": identity.store_id if identity else None,
            "user_id": identity.user_id if identity else None,
            "role": str(identity.role) if identity else None,
            "agent": str(identity.agent) if identity else None,
            "tool": name[:80],
            "outcome": outcome,
            "duration_ms": duration_ms,
            "args_hash": hashed,
            "policy_version": self.policy.version_text,
            "catalogue_version": str(self.catalogue.version),
            "error_message": (message or None) and message[:MAX_ERROR_CHARS],
        }
        for attempt in (row, {**row, "chat_session_id": None, "store_id": None}):
            try:
                async with self.factory() as session:
                    await session.execute(insert(AgentToolCall).values(**attempt))
                    await session.commit()
                return
            except IntegrityError:
                continue
            except Exception:
                logger.exception("could not write the audit row for %s", name)
                return
