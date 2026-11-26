# app/agentic/common/envelope.py
"""The result envelope every MCP tool returns.

    {"ok": true,  "data": {...}, "as_of": "...", "untrusted_fields": [...], "error": null}
    {"ok": false, "data": null,  "as_of": null,  "untrusted_fields": [],    "error":
        {"code": "not_found", "message": "...", "retryable": false}}

untrusted_fields lists the paths in data whose text came from outside RIC's control
(KB article text, incident notes); agents treat that text as data, never as instructions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agentic.values import RETRYABLE_ERRORS, ErrorCode


class ToolError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: ErrorCode
    message: str
    retryable: bool = False


class ToolEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    data: dict[str, Any] | None = None
    as_of: datetime | None = None
    untrusted_fields: list[str] = Field(default_factory=list)
    error: ToolError | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ToolEnvelope:
        if self.ok and (self.error is not None or self.data is None or self.as_of is None):
            raise ValueError("a successful result needs data and as_of, and no error")
        if not self.ok and (self.error is None or self.data is not None):
            raise ValueError("a failed result needs an error and no data")
        return self


def success(
    data: dict[str, Any], as_of: datetime, untrusted_fields: list[str] | None = None
) -> ToolEnvelope:
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    return ToolEnvelope(ok=True, data=data, as_of=as_of, untrusted_fields=untrusted_fields or [])


def failure(code: ErrorCode | str, message: str) -> ToolEnvelope:
    error_code = ErrorCode(code)
    return ToolEnvelope(
        ok=False,
        error=ToolError(code=error_code, message=message, retryable=error_code in RETRYABLE_ERRORS),
    )
