"""The deterministic gate. Agents propose; this decides what is allowed to ship.

The manager agent's verdict can only be made *stricter* by the policy, never looser.
The same static checks run again in the target repo's CI (`cleaner-crew verify`), so a
bug in the crew or tampering with its config can't get a change past them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from functools import lru_cache
from pathlib import Path

import yaml

from . import CREW_DIR
from .models import Category, DiffStats, Verdict

DEFAULT_TEST_GLOBS = [
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**",
    "**/__tests__/**",
    "**/*.test.*",
    "**/*.spec.*",
    "**/*_test.go",
    "**/spec/**",
]

# The crew must never change its own rules or agent definitions, whatever policy.yml says.
CREW_PATHS = [".cleaner-crew/**", ".claude/**", ".git/**", ".mcp.json"]

# Fraction of a size limit above which a passing change is still downgraded to a draft MR.
NEAR_LIMIT = 0.8


@lru_cache(maxsize=512)
def _glob_re(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-ish glob (`**`, `*`, `?`) into a regex over posix paths."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append(r"(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, patterns: list[str]) -> bool:
    path = path.removeprefix("./")
    return any(_glob_re(p).match(path) for p in patterns)


class TrustLevel(str, Enum):
    """How much autonomy a category has. Ordered from least to most."""

    SHADOW = "shadow"  # plan only; the plan is posted on the ticket for a human
    DRAFT = "draft"    # MRs are always opened as drafts
    READY = "ready"    # MRs are opened ready for review when every gate passes

    @property
    def rank(self) -> int:
        return list(TrustLevel).index(self)

    def cap(self, ceiling: TrustLevel) -> TrustLevel:
        return self if self.rank <= ceiling.rank else ceiling


@dataclass
class CategoryRule:
    enabled: bool = True
    tests_pass: bool = True
    new_or_changed_test: bool = True
    require_repro_test: bool = False
    require_benchmark: bool = False
    mutation_testing: bool = True  # only applies when new_or_changed_test is on
    max_level: TrustLevel = TrustLevel.READY  # ceiling a human sets; earned level is capped by it
    trust_since: date | None = None  # ignore MR history before this date (reset after a demotion)
    allow_paths: list[str] = field(default_factory=list)  # exempt from forbidden_paths
    allowed: list[str] = field(default_factory=list)  # e.g. dependency bump kinds: patch, minor

    def __post_init__(self) -> None:
        self.max_level = TrustLevel(self.max_level)
        if isinstance(self.trust_since, str):
            self.trust_since = date.fromisoformat(self.trust_since)


@dataclass
class TrustConfig:
    window: int = 20           # last N closed crew MRs per category
    min_samples: int = 5       # below this, a category stays at draft
    promote_at: float = 0.8    # merged-without-changes rate to earn "ready"
    demote_below: float = 0.5  # below this rate a category falls back to "shadow"


@dataclass
class MutationConfig:
    enabled: bool = True
    max_mutants: int = 8
    min_score: float = 0.5     # fraction of mutants the tests must kill, else draft


@dataclass
class Policy:
    max_files_changed: int = 8
    max_diff_lines: int = 300
    max_open_mrs: int = 3
    max_open_proposals: int = 10
    max_cost_per_task_usd: float = 2.0
    forbidden_paths: list[str] = field(default_factory=list)
    test_globs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))
    hooded_approval: bool = True
    categories: dict[str, CategoryRule] = field(default_factory=dict)
    trust: TrustConfig = field(default_factory=TrustConfig)
    mutation: MutationConfig = field(default_factory=MutationConfig)

    @classmethod
    def load(cls, root: Path) -> Policy:
        return cls.from_yaml((root / CREW_DIR / "policy.yml").read_text())

    @classmethod
    def from_yaml(cls, text: str) -> Policy:
        return cls.from_dict(yaml.safe_load(text) or {})

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        limits = d.get("limits", {})
        require = d.get("require", {})
        cats = {k: CategoryRule(**(v or {})) for k, v in (d.get("categories") or {}).items()}
        return cls(
            max_files_changed=limits.get("max_files_changed", 8),
            max_diff_lines=limits.get("max_diff_lines", 300),
            max_open_mrs=limits.get("max_open_mrs", 3),
            max_open_proposals=limits.get("max_open_proposals", 10),
            max_cost_per_task_usd=limits.get("max_cost_per_task_usd", 2.0),
            forbidden_paths=d.get("forbidden_paths", []),
            test_globs=d.get("test_globs", list(DEFAULT_TEST_GLOBS)),
            hooded_approval=require.get("hooded_approval", True),
            categories=cats,
            trust=TrustConfig(**(d.get("trust") or {})),
            mutation=MutationConfig(**(d.get("mutation") or {})),
        )

    def rule(self, category: Category) -> CategoryRule:
        return self.categories.get(category.value, CategoryRule(enabled=False))

    def is_forbidden(self, path: str, category: Category | None = None) -> bool:
        if matches(path, CREW_PATHS):
            return True
        if category is not None and matches(path, self.rule(category).allow_paths):
            return False
        return matches(path, self.forbidden_paths)

    def is_test(self, path: str) -> bool:
        return matches(path, self.test_globs)


@dataclass
class Evidence:
    """What actually happened during the run, gathered by the orchestrator (not self-reported)."""

    category: Category
    diff: DiffStats
    tests_passed: bool
    repro_confirmed: bool | None = None  # None = not attempted
    benchmark_reported: bool = False
    hooded_approved: bool = False
    hooded_max_severity: str = "none"  # none|low|medium|high|critical
    mutation_score: float | None = None  # None = not run / no mutable lines


@dataclass
class GateResult:
    verdict: Verdict
    reasons: list[str]

    @property
    def passed(self) -> bool:
        return self.verdict in (Verdict.MR, Verdict.DRAFT)


class _Gate:
    def __init__(self) -> None:
        self.verdict, self.reasons = Verdict.MR, []

    def bump(self, v: Verdict, why: str) -> None:
        self.verdict = self.verdict.stricter(v)
        self.reasons.append(why)

    def result(self) -> GateResult:
        return GateResult(self.verdict, self.reasons)


def _static(policy: Policy, category: Category, diff: DiffStats, g: _Gate) -> None:
    """Checks that only need the diff. Shared by the orchestrator and `cleaner-crew verify`."""
    rule = policy.rule(category)
    if not rule.enabled:
        g.bump(Verdict.REJECT, f"category '{category.value}' is not enabled in policy")

    if not diff.files:
        g.bump(Verdict.REJECT, "no changes were produced")

    forbidden = [p for p in diff.paths if policy.is_forbidden(p, category)]
    if forbidden:
        g.bump(Verdict.ESCALATE, f"touches forbidden paths: {', '.join(forbidden)}")

    n_files, n_lines = len(diff.files), diff.total_lines
    if n_files > policy.max_files_changed:
        g.bump(Verdict.ESCALATE, f"{n_files} files changed (limit {policy.max_files_changed})")
    elif n_files > policy.max_files_changed * NEAR_LIMIT:
        g.bump(Verdict.DRAFT, f"{n_files} files changed, close to limit")
    if n_lines > policy.max_diff_lines:
        g.bump(Verdict.ESCALATE, f"{n_lines} diff lines (limit {policy.max_diff_lines})")
    elif n_lines > policy.max_diff_lines * NEAR_LIMIT:
        g.bump(Verdict.DRAFT, f"{n_lines} diff lines, close to limit")

    if rule.new_or_changed_test and not any(policy.is_test(p) for p in diff.paths):
        g.bump(Verdict.REJECT, "no new or changed test covers the change")


def check_static(policy: Policy, category: Category, diff: DiffStats) -> GateResult:
    g = _Gate()
    _static(policy, category, diff, g)
    return g.result()


def evaluate(policy: Policy, ev: Evidence) -> GateResult:
    rule = policy.rule(ev.category)
    g = _Gate()
    _static(policy, ev.category, ev.diff, g)

    if rule.tests_pass and not ev.tests_passed:
        g.bump(Verdict.REJECT, "test suite does not pass")

    if rule.require_repro_test and ev.repro_confirmed is not True:
        g.bump(Verdict.ESCALATE, "bug could not be reproduced with a failing test first")

    if rule.require_benchmark and not ev.benchmark_reported:
        g.bump(Verdict.DRAFT, "no benchmark evidence for performance change")

    if ev.mutation_score is not None and ev.mutation_score < policy.mutation.min_score:
        g.bump(Verdict.DRAFT, f"tests kill only {ev.mutation_score:.0%} of mutants on changed "
                              f"lines (need {policy.mutation.min_score:.0%})")

    if policy.hooded_approval:
        if ev.hooded_max_severity in ("high", "critical"):
            g.bump(Verdict.REJECT, f"security review found {ev.hooded_max_severity} severity issue")
        elif not ev.hooded_approved:
            g.bump(Verdict.DRAFT, "security review did not approve")
        elif ev.hooded_max_severity == "medium":
            g.bump(Verdict.DRAFT, "security review raised a medium severity note")

    return g.result()
