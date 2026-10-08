"""Lightweight, language-agnostic mutation testing on the lines a change touched.

The point is not a full mutation score for the repo. It answers one question: *would the
tests notice if this specific change were subtly wrong?* If the inspector's tests don't
fail when `<` becomes `<=` on the fixed line, they aren't testing the fix.

Mutations are textual, so they work for any language with C-like or Python-like
operators. Mutants that don't compile simply count as killed, which is why min_score
is conservative by default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .detect import run_command
from .policy import Policy

# A mutant whose tests run this much slower than the unmutated suite counts as killed
# (it most likely hangs). The floor absorbs start-up noise on fast suites.
MUTANT_SLOWDOWN = 3
MUTANT_MIN_TIMEOUT_S = 30


def mutant_timeout(baseline_s: float, cap_s: int) -> int:
    """Per-mutant test timeout, from how long the unmutated suite took."""
    return min(cap_s, max(MUTANT_MIN_TIMEOUT_S, round(MUTANT_SLOWDOWN * baseline_s)))

# (pattern, replacement, label). Operators need surrounding spaces to avoid generics/arrows.
OPERATORS: list[tuple[str, str, str]] = [
    (r" == ", " != ", "== -> !="),
    (r" != ", " == ", "!= -> =="),
    (r" === ", " !== ", "=== -> !=="),
    (r" !== ", " === ", "!== -> ==="),
    (r" <= ", " < ", "<= -> <"),
    (r" >= ", " > ", ">= -> >"),
    (r" < ", " <= ", "< -> <="),
    (r" > ", " >= ", "> -> >="),
    (r" and ", " or ", "and -> or"),
    (r" or ", " and ", "or -> and"),
    (r" && ", " || ", "&& -> ||"),
    (r" \|\| ", " && ", "|| -> &&"),
    (r"\bTrue\b", "False", "True -> False"),
    (r"\bFalse\b", "True", "False -> True"),
    (r"\btrue\b", "false", "true -> false"),
    (r"\bfalse\b", "true", "false -> true"),
    (r" \+ ", " - ", "+ -> -"),
    (r" - ", " + ", "- -> +"),
    (r"\bnot ", "", "remove not"),
    (r"(?<![\w.])(\d+)(?![\w.])", None, "n -> n+1"),  # replacement computed
]

COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|--|;)")
NON_CODE = {".md", ".rst", ".txt", ".json", ".yml", ".yaml", ".toml", ".lock", ".cfg", ".ini",
            ".html", ".css", ".scss", ".svg", ".csv", ".xml", ".env", ".sql"}


@dataclass
class Mutant:
    path: str
    line: int
    label: str
    original: str
    mutated: str


@dataclass
class MutationReport:
    total: int = 0
    killed: int = 0
    survivors: list[Mutant] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        return self.killed / self.total if self.total else None

    def summary(self) -> str:
        if not self.total:
            return "no mutable lines"
        s = f"{self.killed}/{self.total} mutants killed"
        if self.survivors:
            s += "; survived: " + ", ".join(
                f"{m.path}:{m.line} ({m.label})" for m in self.survivors[:5])
        return s


def _in_string(line: str, pos: int) -> bool:
    before = line[:pos]
    return before.count('"') % 2 == 1 or before.count("'") % 2 == 1 or before.count("`") % 2 == 1


def mutants_for_line(path: str, lineno: int, line: str) -> list[Mutant]:
    if not line.strip() or COMMENT_RE.match(line):
        return []
    out = []
    for pattern, repl, label in OPERATORS:
        m = next((m for m in re.finditer(pattern, line) if not _in_string(line, m.start())), None)
        if m is None:
            continue
        new = repl if repl is not None else str(int(m.group(1)) + 1)
        mutated = line[: m.start()] + new + line[m.end():]
        if mutated != line:
            out.append(Mutant(path, lineno, label, line, mutated))
    return out


def select(mutants: list[Mutant], limit: int) -> list[Mutant]:
    """Spread the budget across lines instead of spending it all on the first one."""
    by_line: dict[tuple[str, int], list[Mutant]] = {}
    for m in mutants:
        by_line.setdefault((m.path, m.line), []).append(m)
    picked, depth = [], 0
    while len(picked) < limit and any(depth < len(v) for v in by_line.values()):
        for v in by_line.values():
            if depth < len(v) and len(picked) < limit:
                picked.append(v[depth])
        depth += 1
    return picked


def generate(wt: Path, changed: dict[str, list[int]], policy: Policy) -> list[Mutant]:
    mutants = []
    for path, lines in changed.items():
        if policy.is_test(path) or policy.is_forbidden(path) or Path(path).suffix in NON_CODE:
            continue
        f = wt / path
        if not f.is_file():
            continue
        try:
            text = f.read_text().splitlines()
        except UnicodeDecodeError:
            continue
        for n in lines:
            if 1 <= n <= len(text):
                mutants += mutants_for_line(path, n, text[n - 1])
    return select(mutants, policy.mutation.max_mutants)


def run(wt: Path, changed: dict[str, list[int]], policy: Policy, test_cmd: str,
        timeout_s: int) -> MutationReport:
    report = MutationReport()
    for m in generate(wt, changed, policy):
        f = wt / m.path
        original = f.read_text()
        lines = original.splitlines(keepends=True)
        eol = lines[m.line - 1][len(lines[m.line - 1].rstrip("\r\n")):]
        lines[m.line - 1] = m.mutated + eol
        try:
            f.write_text("".join(lines))
            killed = not run_command(test_cmd, wt, timeout_s).ok
        finally:
            f.write_text(original)
        report.total += 1
        if killed:
            report.killed += 1
        else:
            report.survivors.append(m)
    return report
