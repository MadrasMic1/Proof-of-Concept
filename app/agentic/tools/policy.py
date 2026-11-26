# app/agentic/tools/policy.py
"""The tool policy (config/tool_policy.yaml): agent -> tools, role -> denied tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agentic.values import AgentRole, SpecialistName


class PolicyError(ValueError):
    pass


class ToolPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    roles: list[str]
    agents: dict[str, list[str]]
    role_deny: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _known_names(self) -> ToolPolicy:
        roles = {r.value for r in AgentRole}
        agents = {a.value for a in SpecialistName}
        if set(self.roles) != roles:
            raise ValueError(f"roles must be exactly {sorted(roles)}")
        if set(self.agents) != agents:
            raise ValueError(f"agents must be exactly {sorted(agents)}")
        unknown = set(self.role_deny) - roles
        if unknown:
            raise ValueError(f"role_deny has unknown roles {sorted(unknown)}")
        return self

    @property
    def version_text(self) -> str:
        return str(self.version)

    def all_tools(self) -> set[str]:
        tools = {t for names in self.agents.values() for t in names}
        return tools | {t for names in self.role_deny.values() for t in names}

    def allows(self, agent: str, role: str, tool: str) -> bool:
        return tool in self.agents.get(str(agent), []) and tool not in self.role_deny.get(
            str(role), []
        )

    def tools_for(self, agent: str, role: str) -> list[str]:
        return [t for t in self.agents.get(str(agent), []) if self.allows(agent, role, t)]

    def problems_against(self, registered: Iterable[str]) -> list[str]:
        names = set(registered)
        problems = [f"policy names unknown tool {t!r}" for t in sorted(self.all_tools() - names)]
        problems += [f"tool {t!r} is not in the policy" for t in sorted(names - self.all_tools())]
        return problems


def load_policy(path: Path) -> ToolPolicy:
    if not path.is_file():
        raise PolicyError(f"tool policy not found: {path}")
    try:
        return ToolPolicy.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise PolicyError(f"invalid tool policy {path}: {exc}") from exc
