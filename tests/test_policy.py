from importlib import resources

import pytest
import yaml

from cleaner_crew.models import Category, DiffStats, FileChange, Verdict
from cleaner_crew.policy import Evidence, Policy, evaluate, matches


@pytest.fixture
def policy() -> Policy:
    text = (resources.files("cleaner_crew.templates") / "policy.yml").read_text()
    return Policy.from_dict(yaml.safe_load(text))


def diff(*files: tuple[str, int]) -> DiffStats:
    return DiffStats([FileChange(p, n, 0) for p, n in files])


def good(**overrides) -> Evidence:
    ev = Evidence(
        category=Category.BUGFIX,
        diff=diff(("src/app/util.py", 5), ("tests/test_util.py", 12)),
        tests_passed=True, repro_confirmed=True, hooded_approved=True)
    for k, v in overrides.items():
        setattr(ev, k, v)
    return ev


@pytest.mark.parametrize("path,pattern,expected", [
    ("app/migrations/0001.py", "**/migrations/**", True),
    ("migrations/0001.py", "**/migrations/**", True),
    (".github/workflows/ci.yml", ".github/**", True),
    ("src/github/client.py", ".github/**", False),
    ("uv.lock", "**/uv.lock", True),
    ("pkg/uv.lock", "**/uv.lock", True),
    ("src/auth.py", "**/auth/**", False),
    ("./src/auth/login.py", "**/auth/**", True),
])
def test_glob_matching(path, pattern, expected):
    assert matches(path, [pattern]) is expected


def test_clean_bugfix_ships(policy):
    assert evaluate(policy, good()).verdict is Verdict.MR


def test_failing_tests_reject(policy):
    assert evaluate(policy, good(tests_passed=False)).verdict is Verdict.REJECT


def test_bugfix_without_repro_escalates(policy):
    assert evaluate(policy, good(repro_confirmed=False)).verdict is Verdict.ESCALATE


def test_forbidden_path_escalates(policy):
    ev = good(diff=diff(("app/migrations/0002.py", 3), ("tests/test_m.py", 3)))
    res = evaluate(policy, ev)
    assert res.verdict is Verdict.ESCALATE
    assert any("forbidden" in r for r in res.reasons)


def test_lockfile_allowed_only_for_dependency_upgrades(policy):
    assert policy.is_forbidden("uv.lock", Category.BUGFIX)
    assert not policy.is_forbidden("uv.lock", Category.DEPENDENCY_UPGRADE)


def test_missing_test_rejects_bugfix_but_not_docs(policy):
    no_test = diff(("src/app/util.py", 5))
    assert evaluate(policy, good(diff=no_test)).verdict is Verdict.REJECT
    docs = Evidence(Category.DOCS_GAP, diff(("README.md", 10)), tests_passed=True,
                    hooded_approved=True)
    assert evaluate(policy, docs).verdict is Verdict.MR


def test_size_limits(policy):
    big = diff(*[(f"src/f{i}.py", 1) for i in range(9)], ("tests/test_x.py", 1))
    assert evaluate(policy, good(diff=big)).verdict is Verdict.ESCALATE
    near = diff(("src/a.py", 250), ("tests/test_a.py", 1))
    assert evaluate(policy, good(diff=near)).verdict is Verdict.DRAFT


def test_security_review(policy):
    assert evaluate(policy, good(hooded_max_severity="high")).verdict is Verdict.REJECT
    assert evaluate(policy, good(hooded_max_severity="medium")).verdict is Verdict.DRAFT
    assert evaluate(policy, good(hooded_approved=False)).verdict is Verdict.DRAFT


def test_disabled_category_rejects(policy):
    ev = good(category=Category.PERF)
    assert evaluate(policy, ev).verdict is Verdict.REJECT


def test_verdict_stricter():
    assert Verdict.MR.stricter(Verdict.DRAFT) is Verdict.DRAFT
    assert Verdict.REJECT.stricter(Verdict.MR) is Verdict.REJECT
