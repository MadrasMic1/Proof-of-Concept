# tests/agentic/test_agent_spike.py
"""Phase 1: the MCP + token path of the spike (no LLM; a local server on 127.0.0.1)."""

from __future__ import annotations

import pytest

from scripts.agent_spike import check_mcp_token, first_param


async def test_token_travels_in_the_header_and_forged_tokens_are_refused() -> None:
    ok, detail = await check_mcp_token()
    assert ok, detail


def test_first_param() -> None:
    def target(model: str, client: object) -> None: ...

    assert first_param(target, "model_id", "model") == "model"
    with pytest.raises(RuntimeError, match="chat_client"):
        first_param(target, "chat_client")
