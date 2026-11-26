# app/agentic/kb/articles.py
"""KB articles: the structured format, loading, validation and chunking. Pure.

An article is a small decision graph. Each step has an instruction, a question
("ask") and a fixed set of outcomes; each outcome either names the next step or ends
the guide (resolved / exhausted / needs_technician). Two outcomes exist on every step
without being written in the article: "unclear" (ask the step again) and
"user_cannot_do" (end as needs_technician). The KB Guide agent only chooses an
outcome; where it leads is fixed here and enforced by app/agentic/kb/guide.py.

Validation is all-or-nothing (scripts/kb_ingest.py publishes nothing when any
article has a problem) and includes coverage: every fault code the catalogue marks
staff_can_fix must have a published article for its type.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.agentic.store_graph.catalogue import Catalogue

UNCLEAR = "unclear"
USER_CANNOT_DO = "user_cannot_do"
UNIVERSAL_OUTCOMES: dict[str, str] = {
    UNCLEAR: "Not sure / something else",
    USER_CANNOT_DO: "I can't do this step",
}
RESOLVED = "resolved"
HANDOFF_ENDS = frozenset({"exhausted", "needs_technician"})
STAFF = "store_staff"
TECHNICIAN = "technician"

KB_REF = re.compile(r"^KB-[A-Z0-9]+-[0-9]{3}$")
STEP_ID = re.compile(r"^S[0-9]{1,3}$")
KEY = re.compile(r"^[a-z][a-z0-9_]*$")
TAG = re.compile(r"^[a-z][a-z0-9-]*$")
INJECTION = (
    re.compile(r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+instructions", re.I),
    re.compile(r"\b(system|assistant|developer)\s*:", re.I),
    re.compile(r"\b(kb|itsm|store|graph|network)\.[a-z_]+\s*\(", re.I),
    re.compile(r"https?://", re.I),
    re.compile(r"<\s*/?\s*(script|untrusted|system)\b", re.I),
)

EndStatus = Literal["resolved", "exhausted", "needs_technician"]
Audience = Literal["store_staff", "technician"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Outcome(_Model):
    label: str = Field(min_length=1)
    next: str | None = None
    end: EndStatus | None = None

    @model_validator(mode="after")
    def _one_target(self) -> Outcome:
        if (self.next is None) == (self.end is None):
            raise ValueError("an outcome needs exactly one of 'next' or 'end'")
        return self


class Step(_Model):
    instruction: str = Field(min_length=1)
    ask: str = Field(min_length=1)
    audience: Audience | None = None
    outcomes: dict[str, Outcome] = Field(min_length=1)


class Article(_Model):
    kb_ref: str
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=400)
    applies_to: list[str] = Field(min_length=1)
    fault_codes: list[str] = Field(default_factory=list)
    kb_tags: list[str] = Field(default_factory=list)
    symptoms: list[str] = Field(default_factory=list)
    audience: Audience = STAFF
    estimated_minutes: int = Field(default=5, ge=1, le=120)
    start: str
    steps: dict[str, Step] = Field(min_length=1)

    def step_audience(self, step_id: str) -> str:
        return self.steps[step_id].audience or self.audience

    def content(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def content_hash(self) -> str:
        canonical = json.dumps(self.content(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Limits:
    instruction: int
    ask: int
    label: int


@dataclass(frozen=True)
class ChunkSpec:
    chunk_type: Literal["article", "step"]
    step_id: str | None
    text: str
    text_hash: str


# --- loading ----------------------------------------------------------------------------


def load_articles(directory: Path) -> tuple[list[Article], list[str]]:
    """Every *.yaml under directory, sorted by file name, and the problems found."""
    if not directory.is_dir():
        return [], [f"articles folder not found: {directory}"]
    articles: list[Article] = []
    problems: list[str] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            article = Article.model_validate(raw)
        except (yaml.YAMLError, ValidationError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        if not path.name.startswith(f"{article.kb_ref}_"):
            problems.append(f"{path.name}: the file name must start with '{article.kb_ref}_'")
        articles.append(article)
    if not articles and not problems:
        problems.append(f"no articles (*.yaml) in {directory}")
    return articles, problems


# --- validation ---------------------------------------------------------------------------


def _reachable(article: Article) -> set[str]:
    seen: set[str] = set()
    todo = [article.start]
    while todo:
        step_id = todo.pop()
        if step_id in seen or step_id not in article.steps:
            continue
        seen.add(step_id)
        todo += [o.next for o in article.steps[step_id].outcomes.values() if o.next]
    return seen


def _loop(article: Article) -> list[str] | None:
    state: dict[str, int] = {}

    def visit(step_id: str, path: list[str]) -> list[str] | None:
        state[step_id] = 1
        for outcome in article.steps[step_id].outcomes.values():
            target = outcome.next
            if target is None or target not in article.steps:
                continue
            if state.get(target) == 1:
                return [*path, step_id, target]
            if target not in state:
                found = visit(target, [*path, step_id])
                if found:
                    return found
        state[step_id] = 2
        return None

    for step_id in article.steps:
        if step_id not in state:
            found = visit(step_id, [])
            if found:
                return found
    return None


def _texts(article: Article) -> Iterable[tuple[str, str]]:
    yield "title", article.title
    yield "summary", article.summary
    for symptom in article.symptoms:
        yield "symptom", symptom
    for step_id, step in article.steps.items():
        yield f"{step_id}.instruction", step.instruction
        yield f"{step_id}.ask", step.ask
        for key, outcome in step.outcomes.items():
            yield f"{step_id}.{key}.label", outcome.label


def validate_article(article: Article, catalogue: Catalogue, limits: Limits) -> list[str]:
    ref = article.kb_ref
    problems: list[str] = []
    if not KB_REF.fullmatch(ref):
        problems.append(f"{ref}: kb_ref must look like KB-PRN-001")
    for step_id, step in article.steps.items():
        if not STEP_ID.fullmatch(step_id):
            problems.append(f"{ref}: step id {step_id!r} must look like S1")
        if len(step.instruction) > limits.instruction:
            problems.append(f"{ref} {step_id}: instruction longer than {limits.instruction}")
        if len(step.ask) > limits.ask:
            problems.append(f"{ref} {step_id}: ask longer than {limits.ask}")
        labels = [o.label.strip().lower() for o in step.outcomes.values()]
        if len(labels) != len(set(labels)):
            problems.append(f"{ref} {step_id}: outcome labels must be different")
        for key, outcome in step.outcomes.items():
            if key in UNIVERSAL_OUTCOMES:
                problems.append(f"{ref} {step_id}: outcome {key!r} is reserved")
            elif not KEY.fullmatch(key):
                problems.append(f"{ref} {step_id}: outcome {key!r} must be a lowercase key")
            if len(outcome.label) > limits.label:
                problems.append(f"{ref} {step_id}.{key}: label longer than {limits.label}")
            if outcome.next is not None and outcome.next not in article.steps:
                problems.append(f"{ref} {step_id}.{key}: next step {outcome.next!r} not found")

    if article.start not in article.steps:
        problems.append(f"{ref}: start step {article.start!r} not found")
        return problems
    if article.step_audience(article.start) != STAFF:
        problems.append(f"{ref}: the start step must be for store staff")
    if article.audience != STAFF:
        problems.append(f"{ref}: the article audience must be store_staff")
    unreachable = sorted(set(article.steps) - _reachable(article))
    if unreachable:
        problems.append(f"{ref}: steps not reachable from {article.start}: {unreachable}")
    loop = _loop(article)
    if loop:
        problems.append(f"{ref}: loop {' -> '.join(loop)}")
    ends = {
        outcome.end
        for step_id in _reachable(article)
        for outcome in article.steps[step_id].outcomes.values()
        if outcome.end
    }
    if RESOLVED not in ends:
        problems.append(f"{ref}: no path ends 'resolved'")
    if not ends & HANDOFF_ENDS:
        problems.append(f"{ref}: no path ends 'exhausted' or 'needs_technician'")

    for type_code in article.applies_to:
        if type_code not in catalogue.types:
            problems.append(f"{ref}: applies_to type {type_code!r} is not in the catalogue")
    known = {
        rule.code: rule
        for type_code in article.applies_to
        if type_code in catalogue.types
        for rule in catalogue.types[type_code].faults
    }
    for code in article.fault_codes:
        rule = known.get(code)
        if rule is None:
            problems.append(f"{ref}: fault code {code!r} is not a fault of {article.applies_to}")
            continue
        missing = sorted(set(rule.kb_tags) - set(article.kb_tags))
        if missing:
            problems.append(f"{ref}: kb_tags must include the catalogue tags {missing} of {code}")
    for tag in article.kb_tags:
        if not TAG.fullmatch(tag):
            problems.append(f"{ref}: kb tag {tag!r} must be lowercase words joined by '-'")
    for where, text in _texts(article):
        for pattern in INJECTION:
            if pattern.search(text):
                problems.append(f"{ref} {where}: text not allowed ({pattern.pattern})")
    return problems


def coverage_gaps(articles: Sequence[Article], catalogue: Catalogue) -> list[str]:
    """Staff-fixable (type, fault code) pairs of the catalogue without an article."""
    covered = {(t, code) for a in articles for t in a.applies_to for code in a.fault_codes}
    gaps: list[str] = []
    for type_code, entry in sorted(catalogue.types.items()):
        for rule in entry.faults:
            if rule.staff_can_fix and (type_code, rule.code) not in covered:
                gaps.append(f"no article for {type_code} {rule.code} (staff can fix)")
    return gaps


def validate_articles(
    articles: Sequence[Article],
    catalogue: Catalogue,
    limits: Limits,
    *,
    allow_coverage_gaps: bool = False,
) -> list[str]:
    problems: list[str] = []
    refs = [a.kb_ref for a in articles]
    for ref in sorted({r for r in refs if refs.count(r) > 1}):
        problems.append(f"{ref}: defined in more than one file")
    for article in articles:
        problems += validate_article(article, catalogue, limits)
    if not allow_coverage_gaps:
        problems += coverage_gaps(articles, catalogue)
    return problems


# --- chunking -----------------------------------------------------------------------------


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_chunks(article: Article, catalogue: Catalogue) -> list[ChunkSpec]:
    """One article chunk, plus one chunk per store-staff step (technician steps are
    never embedded, so they cannot steer which article store staff are offered)."""
    types = ", ".join(
        catalogue.types[t].label if t in catalogue.types else t for t in article.applies_to
    )
    head = f"{article.title}. {article.summary}"
    if article.symptoms:
        head += f" Symptoms: {'; '.join(article.symptoms)}."
    head += f" Applies to: {types}."
    chunks = [ChunkSpec("article", None, head, _hash(head))]
    for step_id, step in article.steps.items():
        if article.step_audience(step_id) != STAFF:
            continue
        options = "; ".join(o.label for o in step.outcomes.values())
        text = f"{article.title} - {step.instruction} {step.ask} Options: {options}."
        chunks.append(ChunkSpec("step", step_id, text, _hash(text)))
    return chunks
