# tests/agentic/agent_script.py
"""A scripted model for agent tests: an httpx MockTransport that answers the OpenAI
chat-completions API step by step. Each step is a function of the request body that
returns a tool call ({"tool": name, "args": {...}}) or text ({"text": "..."}).
Used under the real Agent Framework loop, the real bridge and the real MCP server."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from app.agentic.llm.transport import hx

Step = Callable[[dict[str, Any]], dict[str, Any]]


def last_tool_result(body: dict[str, Any]) -> dict[str, Any]:
    """The envelope of the most recent tool result in the conversation."""
    for message in reversed(body.get("messages", [])):
        if message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            return json.loads(content)
    raise AssertionError("no tool result in the conversation")


class ScriptedModel:
    def __init__(self, steps: list[Step]) -> None:
        self.steps = list(steps)
        self.requests: list[dict[str, Any]] = []

    def transport(self) -> Any:
        return hx.MockTransport(self)

    def __call__(self, request: Any) -> Any:
        body = json.loads(request.content)
        self.requests.append(body)
        if not self.steps:
            raise AssertionError("the scripted model ran out of steps")
        answer = self.steps.pop(0)(body)
        if "tool" in answer:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{len(self.requests)}",
                        "type": "function",
                        "function": {
                            "name": answer["tool"],
                            "arguments": json.dumps(answer.get("args", {})),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": answer["text"]}
            finish = "stop"
        return hx.Response(
            200,
            json={
                "id": f"cmpl-{len(self.requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": "scripted-model",
                "choices": [{"index": 0, "finish_reason": finish, "message": message}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )


def call(tool: str, **args: Any) -> Step:
    return lambda body: {"tool": tool, "args": args}


def text(value: str) -> Step:
    return lambda body: {"text": value}
