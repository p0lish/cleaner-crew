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


@dataclass
class ConnectionReport:
    ok: bool
    service: str
    identity: str = ""
    detail: str = ""
    missing: list[str] = field(default_factory=list)
