"""`cleaner-crew verify`: re-check a crew branch in the target repo's own CI.

Runs as a required status check on `cleaner-crew/*` MRs. The policy is read from the
*base* branch, never from the MR, so a change can't loosen the rules it is judged by.
It does not execute any code from the branch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import CREW_DIR
from .gitutil import branch_commit_messages, diff_stats, git
from .models import TRAILER_CATEGORY, Category, Verdict, is_crew_commit
from .policy import Policy, check_static


@dataclass
class VerifyResult:
    ok: bool
    category: Category | None
    reasons: list[str]


def verify(root: Path, base: str) -> VerifyResult:
    try:
        policy = Policy.from_yaml(git("show", f"origin/{base}:{CREW_DIR}/policy.yml", cwd=root))
    except RuntimeError:
        return VerifyResult(False, None, [f"no {CREW_DIR}/policy.yml on origin/{base}"])

    messages = branch_commit_messages(root, base)
    crew = [m for m in messages if is_crew_commit(m)]
    if not crew:
        return VerifyResult(False, None, ["no crew commits (missing Cleaner-Crew trailers)"])

    cats = {m.group(1) for msg in crew
            for m in re.finditer(rf"^{TRAILER_CATEGORY}: (\S+)$", msg, re.M)}
    if len(cats) != 1:
        return VerifyResult(False, None, [f"expected one category trailer, found {sorted(cats)}"])
    try:
        category = Category(cats.pop())
    except ValueError as e:
        return VerifyResult(False, None, [str(e)])

    gate = check_static(policy, category, diff_stats(root, base))
    reasons = list(gate.reasons)
    human = len(messages) - len(crew)
    if human:
        reasons.append(f"note: {human} human commit(s) on this branch")
    # Near-limit drafts are fine here; the crew already opened those as drafts.
    ok = gate.verdict in (Verdict.MR, Verdict.DRAFT)
    return VerifyResult(ok, category, reasons)
