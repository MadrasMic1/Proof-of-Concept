# app/itsm/errors.py
"""Errors raised by the ITSM client. Messages never contain tokens or passwords.

Same split as the LLM layer: the client does not retry on its own; the caller
(the sync worker) decides whether to retry later.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class ItsmError(Exception):
    """Base class."""

    message: str
    status: int | None = None

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.message}" if self.status else self.message


class ItsmUnavailable(ItsmError):
    """Timeout, connection error, rate limit (429) or server error (5xx): retry later."""


class ItsmRejected(ItsmError):
    """Bad credentials, bad request or an unexpected answer: fix the cause, do not retry."""


class ItsmNotFound(ItsmError):
    """The ticket (or other object) does not exist in the ITSM."""


class ItsmConfigError(ValueError):
    """The ITSM settings in .env are incomplete or inconsistent."""
