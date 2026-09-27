"""Plain data shared between the orchestrator, adapters and the policy engine."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum


class Category(str, Enum):
    BUGFIX = "bugfix"
    DOCS_GAP = "docs-gap"
    PERF = "perf"
    DEPENDENCY_UPGRADE = "dependency-upgrade"
    CLEANUP = "cleanup"


class Verdict(str, Enum):
    """Ordered from most to least permissive; combining verdicts takes the strictest."""

    MR = "mr"
    DRAFT = "draft"
    ESCALATE = "escalate"
    REJECT = "reject"

    @property
    def rank(self) -> int:
        return list(Verdict).index(self)

    def stricter(self, other: Verdict) -> Verdict:
        return self if self.rank >= other.rank else other


@dataclass
class Task:
    """A unit of work in the tracker (Jira issue / Linear issue)."""

    id: str
    key: str  # human-readable: ABC-123
    title: str
    description: str
    url: str
    labels: list[str] = field(default_factory=list)
    fingerprint: str | None = None


@dataclass
class Finding:
    """Something the scout proposes. Becomes a tracker ticket before anyone works on it."""

    title: str
    category: Category
    description: str
    files: list[str]
    effort: str = "small"
    risk: str = "low"

    @property
    def fingerprint(self) -> str:
        norm = re.sub(r"\W+", " ", self.title.lower()).strip()
        raw = f"{self.category.value}|{norm}|{'|'.join(sorted(self.files))}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


FINGERPRINT_MARKER = "cleaner-crew:fp="


def fingerprint_marker(fp: str) -> str:
    # Plain text on purpose: Linear and Jira both strip HTML comments.
    return f"{FINGERPRINT_MARKER}{fp}"


def extract_fingerprint(text: str) -> str | None:
    m = re.search(re.escape(FINGERPRINT_MARKER) + r"([0-9a-f]{16})", text or "")
    return m.group(1) if m else None


@dataclass
class FileChange:
    path: str
    added: int
    removed: int


@dataclass
class DiffStats:
    files: list[FileChange]

    @property
    def paths(self) -> list[str]:
        return [f.path for f in self.files]

    @property
    def total_lines(self) -> int:
        return sum(f.added + f.removed for f in self.files)


TRAILER_TASK = "Cleaner-Crew-Task"
TRAILER_CATEGORY = "Cleaner-Crew-Category"


def crew_trailers(task_key: str, category: Category) -> str:
    """Git trailers on every crew commit. Humans' commits on a crew branch lack them."""
    return f"{TRAILER_TASK}: {task_key}\n{TRAILER_CATEGORY}: {category.value}"


def is_crew_commit(message: str) -> bool:
    return f"\n{TRAILER_TASK}: " in message


def category_label(category: Category) -> str:
    return f"cleaner-crew:{category.value}"


def category_from_labels(labels: list[str]) -> Category | None:
    values = {c.value for c in Category}
    for label in labels:
        name = label.removeprefix("cleaner-crew:")
        if label.startswith("cleaner-crew:") and name in values:
            return Category(name)
    return None


@dataclass
class MrRecord:
    """A closed crew MR, used to measure how much each category can be trusted."""

    category: Category
    merged: bool
    changed_by_human: bool  # a commit without crew trailers was pushed to the branch
    closed_at: str  # ISO date

    @property
    def accepted(self) -> bool:
        return self.merged and not self.changed_by_human


@dataclass
class ConnectionReport:
    ok: bool
    service: str
    identity: str = ""
    detail: str = ""
    missing: list[str] = field(default_factory=list)
