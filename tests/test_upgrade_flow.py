"""End-to-end dependency upgrades and Free-plan safeguards, with the fake claude binary."""

import json
import subprocess
import sys

import pytest
import yaml

from cleaner_crew.config import CodeHostConfig, CommandsConfig, Config, RunConfig, TrackerConfig
from cleaner_crew.models import Category, Task
from cleaner_crew.orchestrator import Crew
from cleaner_crew.policy import Policy, TrustLevel

from .test_orchestrator import TRUSTED, FakeHost, FakeTracker, agents_called, history, repo, sh

__all__ = ["repo"]  # pytest fixture re-export

# Stands in for `npm install <pkg>@<version> --ignore-scripts`: rewrites the lockfile and
# pulls in a new transitive dependency that has an install script.
FAKE_INSTALL = '''
import json, sys
pkg, version = sys.argv[1], sys.argv[2]
lock = json.load(open("package-lock.json"))
lock["packages"][f"node_modules/{pkg}"]["version"] = version
lock["packages"]["node_modules/new-helper"] = {"version": "0.1.0", "hasInstallScript": True}
json.dump(lock, open("package-lock.json", "w"), indent=2)
manifest = json.load(open("package.json"))
manifest["dependencies"][pkg] = "^" + version
json.dump(manifest, open("package.json", "w"), indent=2)
'''


@pytest.fixture
def npm_repo(repo, tmp_path):
    (repo / "package.json").write_text(json.dumps({"name": "app",
                                                   "dependencies": {"left-pad": "^1.0.0"}}))
    (repo / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "": {"name": "app"}, "node_modules/left-pad": {"version": "1.0.0"}}}))
    sh(repo, "git", "add", ".")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "npm")
    sh(repo, "git", "push", "-q", "origin", "main")

    policy = yaml.safe_load((repo / ".cleaner-crew" / "policy.yml").read_text())
    policy["categories"]["dependency-upgrade"]["enabled"] = True
    (repo / ".cleaner-crew" / "policy.yml").write_text(yaml.safe_dump(policy))
    (tmp_path / "fake_install.py").write_text(FAKE_INSTALL)
    return repo


def crew_for(repo, tracker, host, tmp_path, outdated="", scout=False):
    cfg = Config(
        root=repo,
        tracker=TrackerConfig("linear", "ENG", token_env="LINEAR_API_KEY"),
        code_host=CodeHostConfig("github", "acme/app", "", "GITHUB_TOKEN"),
        commands=CommandsConfig(
            test="true", install=f"{sys.executable} {tmp_path / 'fake_install.py'} "
                                 "{package} {version}",
            outdated=outdated),
        run=RunConfig(scout_enabled=scout),
    )
    return Crew(cfg, Policy.load(repo), tracker, host)


def test_minor_upgrade_opens_draft_pr(npm_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "upgrade")
    monkeypatch.setenv("FAKE_TARGET", "1.3.0")
    tracker, host = FakeTracker([Task("9", "ENG-9", "Upgrade left-pad", "", "")]), FakeHost()
    [outcome] = crew_for(npm_repo, tracker, host, tmp_path).run_once()

    assert outcome.verdict == "draft", outcome.reasons  # max_level: draft for upgrades
    assert host.mrs[0]["labels"] == ["cleaner-crew", "cleaner-crew:dependency-upgrade"]
    log = subprocess.run(["git", "log", "--format=%s", "origin/cleaner-crew/eng-9"],
                         cwd=npm_repo, capture_output=True, text=True).stdout.splitlines()
    assert log[:2] == ["fix: adapt to left-pad 1.3.0 (ENG-9)",
                       "chore(deps): bump left-pad from 1.0.0 to 1.3.0"]

    calls = agents_called(npm_repo)
    assert [c["agent"] for c in calls] == ["manager", "quartermaster", "hooded", "manager"]
    hooded_prompt = calls[2]["prompt"]
    assert "left-pad 1.0.0 -> 1.3.0 (minor)" in hooded_prompt
    assert "NEW packages with install scripts (1): new-helper@0.1.0" in hooded_prompt
    assert '"version": "1.3.0"' not in hooded_prompt  # raw lockfile diff is not shown


def test_major_upgrade_escalates_before_installing(npm_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "upgrade")
    monkeypatch.setenv("FAKE_TARGET", "2.0.0")
    tracker, host = FakeTracker([Task("10", "ENG-10", "Upgrade left-pad", "", "")]), FakeHost()
    [outcome] = crew_for(npm_repo, tracker, host, tmp_path).run_once()

    assert outcome.verdict == "escalate"
    assert "major upgrade" in outcome.reasons[0]
    assert [c["agent"] for c in agents_called(npm_repo)] == ["manager"]
    assert host.mrs == []


def test_scout_proposes_allowed_upgrades_without_calling_the_agent(npm_repo, tmp_path):
    outdated = json.dumps({"left-pad": {"current": "1.0.0", "wanted": "1.0.2",
                                        "latest": "2.0.0"},
                           "other": {"current": "3.1.0", "wanted": "3.1.0",
                                     "latest": "3.2.0"}})
    (tmp_path / "outdated.json").write_text(outdated)
    tracker, host = FakeTracker([]), FakeHost(TRUSTED)
    crew = crew_for(npm_repo, tracker, host, tmp_path, outdated=f"cat {tmp_path}/outdated.json",
                    scout=True)
    crew.cfg.run.max_findings_per_scout = 2
    crew.run_once()

    assert [t.title for t in tracker.filed] == ["Upgrade left-pad from 1.0.0 to 1.0.2",
                                                "Upgrade other from 3.1.0 to 3.2.0"]
    assert not (npm_repo.parent / "agents.log").exists()  # quota filled, scout agent skipped


def test_unprotected_default_branch_caps_trust_at_draft(repo):
    host = FakeHost(history(Category.BUGFIX, accepted=10))
    host.branch_protection = lambda branch: (False, "branch protection is unavailable")
    tracker = FakeTracker([Task("11", "ENG-11", "Bug", "", "")])
    from .test_orchestrator import make_crew
    crew = make_crew(repo, tracker, host)
    [outcome] = crew.run_once()

    assert crew.levels[Category.BUGFIX].level is TrustLevel.DRAFT
    assert outcome.verdict == "draft"
    assert any("branch protection is unavailable" in r for r in outcome.reasons)
