import json
from pathlib import Path

import pytest

from cleaner_crew import deps
from cleaner_crew.gitutil import push
from cleaner_crew.hooks.guard import check
from cleaner_crew.models import Category, DiffStats, FileChange, Verdict
from cleaner_crew.policy import CategoryRule, Evidence, Policy, check_static, evaluate


@pytest.mark.parametrize("current,target,kind", [
    ("1.42.1", "1.42.3", "patch"),
    ("1.42.1", "1.55.0", "minor"),
    ("1.42.1", "2.0.0", "major"),
    ("0.3.1", "0.4.0", "major"),   # 0.x minor bumps are breaking
    ("0.3.1", "0.3.2", "patch"),
    ("v2.1", "2.1.1", "patch"),
    ("1.0.0", "1.0.0", "none"),
    ("1.2.0", "1.1.0", "none"),
    ("1.0.0", "1.1.0-beta.1", "unknown"),
    ("git+https://x", "1.0.0", "unknown"),
])
def test_bump_kind(current, target, kind):
    assert deps.bump_kind(current, target) == kind


def test_parse_outdated_npm_and_uv():
    npm = json.dumps({"@playwright/test": {"current": "1.42.1", "wanted": "1.42.1",
                                           "latest": "1.55.0"},
                      "left-pad": [{"current": "1.0.0", "wanted": "1.0.1", "latest": "2.0.0"}]})
    uv = json.dumps([{"name": "httpx", "version": "0.27.0", "latest_version": "0.28.1"}])
    got = {o.package: o for o in deps.parse_outdated(npm) + deps.parse_outdated(uv)}
    assert got["@playwright/test"].target(["patch", "minor"]) == "1.55.0"
    assert got["left-pad"].target(["patch", "minor"]) == "1.0.1"  # latest is a major
    assert got["httpx"].target(["patch", "minor"]) is None       # 0.27 -> 0.28 is major in 0.x
    assert deps.parse_outdated("not json") == []


def _npm_lock(**pkgs) -> str:
    packages = {"": {"name": "app"}}
    for key, spec in pkgs.items():
        version, *flags = spec.split(":")
        packages[f"node_modules/{key.replace('__', '/')}"] = {
            "version": version, **({"hasInstallScript": True} if "script" in flags else {})}
    return json.dumps({"lockfileVersion": 3, "packages": packages})


def test_diff_locks_flags_new_install_scripts():
    before = _npm_lock(a="1.0.0", b="2.0.0")
    after = _npm_lock(a="1.1.0", c="0.1.0:script")
    d = deps.diff_locks("package-lock.json", before, after)
    assert d.changed == ["a 1.0.0 -> 1.1.0"]
    assert d.added == ["c@0.1.0"] and d.removed == ["b@2.0.0"]
    assert d.new_install_scripts == ["c@0.1.0"]
    assert d.summary().startswith("NEW packages with install scripts (1): c@0.1.0")


def test_locked_version(tmp_path: Path):
    (tmp_path / "package-lock.json").write_text(_npm_lock(**{"@scope__pkg": "3.2.1"}))
    assert deps.locked_version(tmp_path, "@scope/pkg") == "3.2.1"
    assert deps.locked_version(tmp_path, "missing") is None
    uv = tmp_path / "uvproj"
    uv.mkdir()
    (uv / "uv.lock").write_text('version = 1\n\n[[package]]\nname = "httpx"\nversion = "0.28.1"\n')
    assert deps.locked_version(uv, "httpx") == "0.28.1"


# -- policy -------------------------------------------------------------------------


def _policy() -> Policy:
    return Policy(forbidden_paths=["**/package-lock.json"], categories={
        "dependency-upgrade": CategoryRule(allowed=["patch", "minor"],
                                           allow_paths=["**/package-lock.json"]),
        "bugfix": CategoryRule()})


def test_lockfiles_do_not_count_towards_size_limits():
    diff = DiffStats([FileChange("package-lock.json", 4000, 3000),
                      FileChange("package.json", 1, 1)])
    assert check_static(_policy(), Category.DEPENDENCY_UPGRADE, diff).verdict is Verdict.MR


def test_upgrades_need_no_new_tests_but_must_be_allowed_kind():
    diff = DiffStats([FileChange("package.json", 1, 1)])
    ev = Evidence(Category.DEPENDENCY_UPGRADE, diff, tests_passed=True, hooded_approved=True,
                  bump_kind="minor")
    assert evaluate(_policy(), ev).verdict is Verdict.MR
    ev.bump_kind = "major"
    assert evaluate(_policy(), ev).verdict is Verdict.ESCALATE


def test_lockfile_still_forbidden_for_other_categories():
    diff = DiffStats([FileChange("package-lock.json", 1, 1), FileChange("tests/test_a.py", 1, 0)])
    assert check_static(_policy(), Category.BUGFIX, diff).verdict is Verdict.ESCALATE


# -- guard + push -------------------------------------------------------------------

WT = Path("/repo/wt")


@pytest.mark.parametrize("role", ["janitor", "quartermaster", "inspector"])
def test_no_agent_writes_lockfiles(role):
    reason = check({"tool_name": "Write", "tool_input": {"file_path": f"{WT}/package-lock.json"}},
                   role, WT, _policy(), Category.DEPENDENCY_UPGRADE)
    assert reason and "lockfile" in reason


def test_quartermaster_cannot_edit_tests_but_can_edit_code():
    def write(path):
        return check({"tool_name": "Edit", "tool_input": {"file_path": f"{WT}/{path}"}},
                     "quartermaster", WT, _policy(), Category.DEPENDENCY_UPGRADE)
    assert "inspector" in write("tests/test_x.py")
    assert write("src/app.ts") is None
    assert write("package.json") is None


@pytest.mark.parametrize("cmd", ["npm install left-pad", "npm i", "pnpm add x", "yarn add x",
                                 "pip install requests", "uv add httpx", "npx -y cowsay"])
def test_agents_cannot_install_packages(cmd):
    assert check({"tool_name": "Bash", "tool_input": {"command": cmd}}, "quartermaster", WT,
                 _policy(), None)


@pytest.mark.parametrize("branch", ["main", "master", "cleaner-crew/", "feature/x"])
def test_push_refuses_non_crew_branches(tmp_path, branch):
    with pytest.raises(RuntimeError, match="refusing to push"):
        push(tmp_path, branch)
