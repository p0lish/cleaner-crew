"""The deterministic gate. Agents propose; this decides what is allowed to ship.

The manager agent's verdict can only be made *stricter* by the policy, never looser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class CategoryRule:
    enabled: bool = True
    tests_pass: bool = True
    new_or_changed_test: bool = True
    require_repro_test: bool = False
    require_benchmark: bool = False
    allow_paths: list[str] = field(default_factory=list)  # exempt from forbidden_paths
    allowed: list[str] = field(default_factory=list)  # e.g. dependency bump kinds: patch, minor


@dataclass
class Policy:
    max_files_changed: int = 8
    max_diff_lines: int = 300
    max_open_mrs: int = 3
    max_cost_per_task_usd: float = 2.0
    forbidden_paths: list[str] = field(default_factory=list)
    test_globs: list[str] = field(default_factory=lambda: list(DEFAULT_TEST_GLOBS))
    hooded_approval: bool = True
    categories: dict[str, CategoryRule] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> Policy:
        return cls.from_dict(yaml.safe_load((root / CREW_DIR / "policy.yml").read_text()) or {})

    @classmethod
    def from_dict(cls, d: dict) -> Policy:
        limits = d.get("limits", {})
        require = d.get("require", {})
        cats = {k: CategoryRule(**(v or {})) for k, v in (d.get("categories") or {}).items()}
        return cls(
            max_files_changed=limits.get("max_files_changed", 8),
            max_diff_lines=limits.get("max_diff_lines", 300),
            max_open_mrs=limits.get("max_open_mrs", 3),
            max_cost_per_task_usd=limits.get("max_cost_per_task_usd", 2.0),
            forbidden_paths=d.get("forbidden_paths", []),
            test_globs=d.get("test_globs", list(DEFAULT_TEST_GLOBS)),
            hooded_approval=require.get("hooded_approval", True),
            categories=cats,
        )

    def rule(self, category: Category) -> CategoryRule:
        return self.categories.get(category.value, CategoryRule(enabled=False))

    def is_forbidden(self, path: str, category: Category | None = None) -> bool:
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


@dataclass
class GateResult:
    verdict: Verdict
    reasons: list[str]

    @property
    def passed(self) -> bool:
        return self.verdict in (Verdict.MR, Verdict.DRAFT)


def evaluate(policy: Policy, ev: Evidence) -> GateResult:
    rule = policy.rule(ev.category)
    verdict, reasons = Verdict.MR, []

    def bump(v: Verdict, why: str) -> None:
        nonlocal verdict
        verdict = verdict.stricter(v)
        reasons.append(why)

    if not rule.enabled:
        bump(Verdict.REJECT, f"category '{ev.category.value}' is not enabled in policy")

    if not ev.diff.files:
        bump(Verdict.REJECT, "no changes were produced")

    forbidden = [p for p in ev.diff.paths if policy.is_forbidden(p, ev.category)]
    if forbidden:
        bump(Verdict.ESCALATE, f"touches forbidden paths: {', '.join(forbidden)}")

    n_files, n_lines = len(ev.diff.files), ev.diff.total_lines
    if n_files > policy.max_files_changed:
        bump(Verdict.ESCALATE, f"{n_files} files changed (limit {policy.max_files_changed})")
    elif n_files > policy.max_files_changed * NEAR_LIMIT:
        bump(Verdict.DRAFT, f"{n_files} files changed, close to limit")
    if n_lines > policy.max_diff_lines:
        bump(Verdict.ESCALATE, f"{n_lines} diff lines (limit {policy.max_diff_lines})")
    elif n_lines > policy.max_diff_lines * NEAR_LIMIT:
        bump(Verdict.DRAFT, f"{n_lines} diff lines, close to limit")

    if rule.tests_pass and not ev.tests_passed:
        bump(Verdict.REJECT, "test suite does not pass")

    if rule.new_or_changed_test and not any(policy.is_test(p) for p in ev.diff.paths):
        bump(Verdict.REJECT, "no new or changed test covers the change")

    if rule.require_repro_test and ev.repro_confirmed is not True:
        bump(Verdict.ESCALATE, "bug could not be reproduced with a failing test first")

    if rule.require_benchmark and not ev.benchmark_reported:
        bump(Verdict.DRAFT, "no benchmark evidence for performance change")

    if policy.hooded_approval:
        if ev.hooded_max_severity in ("high", "critical"):
            bump(Verdict.REJECT, f"security review found {ev.hooded_max_severity} severity issue")
        elif not ev.hooded_approved:
            bump(Verdict.DRAFT, "security review did not approve")
        elif ev.hooded_max_severity == "medium":
            bump(Verdict.DRAFT, "security review raised a medium severity note")

    return GateResult(verdict, reasons)
