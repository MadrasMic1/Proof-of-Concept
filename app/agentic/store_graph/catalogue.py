# app/agentic/store_graph/catalogue.py
"""The element-type catalogue: what each device / network element type means.

config/element_catalogue.yaml is the only place that holds knowledge about types
(synonyms, impact text, fault rules, incident routing). The graph engine stays
generic: a new type is a catalogue entry, not code.

Fault rules: devices carry a free-text status_reason, not a fault code. A rule
matches when one of its words appears in the reason (case-insensitive); the first
matching rule wins, so specific rules go first. No match -> the type's defaults.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

SLUG = r"^[a-z][a-z0-9_]*$"
TAG = r"^[a-z][a-z0-9-]*$"


class CatalogueError(ValueError):
    """The catalogue file is missing, invalid, or does not match the database."""


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _lower_words(values: list[str]) -> list[str]:
    words = [value.strip().lower() for value in values]
    if any(not word for word in words):
        raise ValueError("empty word")
    return words


class FaultRule(_Model):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    match: list[str] = Field(min_length=1)
    staff_can_fix: bool
    kb_tags: list[str] = Field(default_factory=list)

    @field_validator("match")
    @classmethod
    def _match(cls, value: list[str]) -> list[str]:
        return _lower_words(value)

    @field_validator("kb_tags")
    @classmethod
    def _tags(cls, value: list[str]) -> list[str]:
        for tag in value:
            if not re.fullmatch(TAG, tag):
                raise ValueError(f"kb tag {tag!r} must be lowercase words joined by '-'")
        return value


class TypeEntry(_Model):
    kind: Literal["device", "network_element"]
    label: str
    synonyms: list[str] = Field(min_length=1)
    impact_when_down: str
    impact_when_degraded: str
    # A member of a redundancy group is down (see agent_dependencies.yaml):
    impact_when_failover: str | None = None  # the preferred member is down, another is up
    impact_when_reduced: str | None = None  # another member is down, the preferred is up
    impact_when_group_down: str | None = None  # every member is down
    incident_category: Literal["network", "hardware", "software", "inquiry"]
    subcategories: list[str] = Field(min_length=1)
    assignment_group: str
    revenue_critical: bool
    staff_can_fix: bool = False
    kb_tags: list[str] = Field(default_factory=list)
    faults: list[FaultRule] = Field(default_factory=list)

    @field_validator("synonyms")
    @classmethod
    def _synonyms(cls, value: list[str]) -> list[str]:
        return _lower_words(value)

    @model_validator(mode="after")
    def _unique_faults(self) -> TypeEntry:
        codes = [rule.code for rule in self.faults]
        if len(codes) != len(set(codes)):
            raise ValueError("fault codes must be unique within a type")
        return self


class ServiceEntry(_Model):
    label: str
    synonyms: list[str] = Field(default_factory=list)
    # The service works while the store's redundancy group of this name works.
    via_group: str = Field(pattern=SLUG)
    impact_when_down: str
    impact_when_degraded: str

    @field_validator("synonyms")
    @classmethod
    def _synonyms(cls, value: list[str]) -> list[str]:
        return _lower_words(value)


@dataclass(frozen=True)
class Classification:
    fault_code: str | None
    staff_can_fix: bool
    kb_tags: tuple[str, ...]


class Catalogue(_Model):
    version: int = Field(ge=1)
    types: dict[str, TypeEntry]
    services: dict[str, ServiceEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys(self) -> Catalogue:
        for key in [*self.types, *self.services]:
            if not re.fullmatch(SLUG, key):
                raise ValueError(f"catalogue key {key!r} must be a lowercase code")
        both = set(self.types) & set(self.services)
        if both:
            raise ValueError(f"keys used as both type and service: {sorted(both)}")
        return self

    def entry(self, type_code: str) -> TypeEntry:
        try:
            return self.types[type_code]
        except KeyError as exc:
            raise CatalogueError(f"type {type_code!r} is not in the catalogue") from exc

    def service(self, code: str) -> ServiceEntry:
        try:
            return self.services[code]
        except KeyError as exc:
            raise CatalogueError(f"service {code!r} is not in the catalogue") from exc

    def classify(self, type_code: str, reason: str | None) -> Classification:
        """Fault code, staff_can_fix and KB tags for a failing node of this type."""
        entry = self.entry(type_code)
        text = (reason or "").lower()
        for rule in entry.faults:
            if any(word in text for word in rule.match):
                return Classification(rule.code, rule.staff_can_fix, tuple(rule.kb_tags))
        return Classification(None, entry.staff_can_fix, tuple(entry.kb_tags))

    def problems_against(
        self,
        *,
        device_types: Iterable[str],
        element_types: Iterable[str],
        assignment_groups: Iterable[str],
    ) -> list[str]:
        """Differences between the catalogue and the master data in the database."""
        problems: list[str] = []
        expected = {code: "device" for code in device_types}
        expected.update({code: "network_element" for code in element_types})
        for code, kind in sorted(expected.items()):
            entry = self.types.get(code)
            if entry is None:
                problems.append(f"{kind} type {code!r} is missing from the catalogue")
            elif entry.kind != kind:
                problems.append(f"type {code!r} is a {kind} in the database, {entry.kind} here")
        for code in sorted(set(self.types) - set(expected)):
            problems.append(f"catalogue type {code!r} does not exist in the database")
        groups = set(assignment_groups)
        for code, entry in sorted(self.types.items()):
            if entry.assignment_group not in groups:
                problems.append(
                    f"type {code!r}: assignment group {entry.assignment_group!r} is unknown"
                )
        return problems


def load_catalogue(path: Path) -> Catalogue:
    if not path.is_file():
        raise CatalogueError(f"catalogue file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Catalogue.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as exc:
        raise CatalogueError(f"invalid catalogue {path}: {exc}") from exc
