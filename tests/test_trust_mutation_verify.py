import subprocess
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from cleaner_crew import mutation, trust
from cleaner_crew.gitutil import changed_lines
from cleaner_crew.models import Category, DiffStats, FileChange, MrRecord, crew_trailers
from cleaner_crew.policy import CategoryRule, Evidence, Policy, TrustLevel, evaluate
from cleaner_crew.verify import verify

# -- trust -----------------------------------------------------------------------


def recs(cat, accepted, merged_changed=0, closed=0, day="2026-09-01"):
    return ([MrRecord(cat, True, False, day)] * accepted
            + [MrRecord(cat, True, True, day)] * merged_changed
            + [MrRecord(cat, False, False, day)] * closed)


def policy_with(**rule) -> Policy:
    return Policy(categories={"bugfix": CategoryRule(**rule)})


@pytest.mark.parametrize("history,level", [
    ([], TrustLevel.DRAFT),                                      # no evidence yet
    (recs(Category.BUGFIX, 3), TrustLevel.DRAFT),                # below min_samples
    (recs(Category.BUGFIX, 8, merged_changed=2), TrustLevel.READY),   # 80%
    (recs(Category.BUGFIX, 6, merged_changed=4), TrustLevel.DRAFT),   # 60%
    (recs(Category.BUGFIX, 2, closed=4), TrustLevel.SHADOW),          # 33%
])
def test_trust_levels(history, level):
    assert trust.compute(policy_with(), history)[Category.BUGFIX].level is level


def test_human_fixups_do_not_count_as_accepted():
    st = trust.compute(policy_with(), recs(Category.BUGFIX, 0, merged_changed=6))
    assert st[Category.BUGFIX].level is TrustLevel.SHADOW


def test_max_level_caps_earned_trust():
    st = trust.compute(policy_with(max_level="draft"), recs(Category.BUGFIX, 10))
    assert (st[Category.BUGFIX].earned, st[Category.BUGFIX].level) == (
        TrustLevel.READY, TrustLevel.DRAFT)


def test_trust_since_resets_history():
    old_failures = recs(Category.BUGFIX, 0, closed=10, day="2026-01-01")
    p = policy_with(trust_since=date(2026, 6, 1))
    assert trust.compute(p, old_failures)[Category.BUGFIX].samples == 0


def test_window_uses_most_recent():
    history = recs(Category.BUGFIX, 20, day="2026-09-01") + recs(
        Category.BUGFIX, 0, closed=20, day="2026-01-01")
    assert trust.compute(policy_with(), history)[Category.BUGFIX].level is TrustLevel.READY


# -- mutation ----------------------------------------------------------------------


def test_mutants_for_line():
    labels = {m.label for m in mutation.mutants_for_line("a.py", 1, "if x < 10 and ok:")}
    assert {"< -> <=", "and -> or", "n -> n+1"} <= labels


def test_no_mutants_in_comments_or_strings():
    assert mutation.mutants_for_line("a.py", 1, "# if x < 10") == []
    assert mutation.mutants_for_line("a.js", 1, "// a == b") == []
    assert mutation.mutants_for_line("a.py", 1, 'msg = "a == b"') == []


def test_select_spreads_across_lines():
    ms = mutation.mutants_for_line("a", 1, "x = a < b and c == 1") + \
        mutation.mutants_for_line("a", 2, "y = d > 2")
    picked = mutation.select(ms, 3)
    assert {m.line for m in picked} == {1, 2}


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "calc.py").write_text("def limit(x):\n    return x\n")
    (tmp_path / ".cleaner-crew").mkdir()
    (tmp_path / ".cleaner-crew" / "policy.yml").write_text(
        (resources.files("cleaner_crew.templates") / "policy.yml").read_text())
    _git(tmp_path, "add", ".")
    _git(tmp_path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(tmp_path, "checkout", "-qb", "cleaner-crew/eng-1")
    return tmp_path


def _commit(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg)


@pytest.mark.parametrize("test_cmd,killed", [
    ("grep -q 'x >= 10' calc.py", 1),  # a test that pins the exact boundary
    ("true", 0),                        # a test that checks nothing
])
def test_mutation_run(tmp_path, test_cmd, killed):
    repo = _repo(tmp_path)
    (repo / "calc.py").write_text("def limit(x):\n    return x >= 10\n")
    _commit(repo, "fix")
    changed = changed_lines(repo, "main")
    assert changed == {"calc.py": [2]}
    policy = Policy()
    policy.mutation.max_mutants = 1
    report = mutation.run(repo, changed, policy, test_cmd, 30)
    assert (report.total, report.killed) == (1, killed)
    assert (repo / "calc.py").read_text() == "def limit(x):\n    return x >= 10\n"  # restored


def test_mutant_timeout_scales_with_the_suite():
    assert mutation.mutant_timeout(0.4, 900) == 30     # fast suite: the floor
    assert mutation.mutant_timeout(100, 900) == 300    # 3x the unmutated run
    assert mutation.mutant_timeout(400, 900) == 900    # never above the configured cap


def test_low_mutation_score_downgrades_to_draft():
    p = Policy(categories={"bugfix": CategoryRule()})
    ev = Evidence(Category.BUGFIX, DiffStats([FileChange("a.py", 1, 0),
                                              FileChange("tests/test_a.py", 1, 0)]),
                  tests_passed=True, hooded_approved=True, mutation_score=0.25)
    assert evaluate(p, ev).verdict.value == "draft"


# -- verify ----------------------------------------------------------------------


def test_verify_passes_clean_crew_branch(tmp_path):
    repo = _repo(tmp_path)
    (repo / "calc.py").write_text("def limit(x):\n    return max(x, 0)\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text("def test(): pass\n")
    _commit(repo, f"fix: clamp\n\n{crew_trailers('ENG-1', Category.BUGFIX)}")
    res = verify(repo, "main")
    assert res.ok, res.reasons
    assert res.category is Category.BUGFIX


def test_verify_fails_without_trailers(tmp_path):
    repo = _repo(tmp_path)
    (repo / "calc.py").write_text("x = 1\n")
    _commit(repo, "sneaky")
    assert not verify(repo, "main").ok


def test_verify_uses_base_policy_not_branch_policy(tmp_path):
    repo = _repo(tmp_path)
    # the branch loosens its own policy and edits a forbidden path
    (repo / ".cleaner-crew" / "policy.yml").write_text("forbidden_paths: []\n")
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("drop table users;\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test(): pass\n")
    _commit(repo, f"fix\n\n{crew_trailers('ENG-1', Category.BUGFIX)}")
    res = verify(repo, "main")
    assert not res.ok
    assert any(".cleaner-crew/policy.yml" in r and "migrations/001.sql" in r
               for r in res.reasons)


def test_verify_rejects_disabled_category(tmp_path):
    repo = _repo(tmp_path)
    (repo / "calc.py").write_text("x = 1\n")
    _commit(repo, f"perf\n\n{crew_trailers('ENG-1', Category.PERF)}")
    assert not verify(repo, "main").ok
