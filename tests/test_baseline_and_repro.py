"""Regression tests for what went wrong with CHA-1 in 4chan-viewer: a test that already
failed in CI was blamed on the crew's fix, and it also faked a "reproduced" bug."""

import pytest

from cleaner_crew.claude import role_tools
from cleaner_crew.models import Task
from cleaner_crew.orchestrator import similar_titles

from .test_orchestrator import TRUSTED, FakeHost, FakeTracker, agents_called, make_crew, repo

__all__ = ["repo"]  # pytest fixture re-export

UNRELATED_FAILURE = "{ echo 'FAIL tests/alerts.test.ts'; false; }"


def task(n: int) -> Task:
    return Task(str(n), f"ENG-{n}", "Bug", "", "")


def test_red_baseline_stops_the_run_before_claiming_anything(repo):
    tracker, host = FakeTracker([task(20)]), FakeHost(TRUSTED)
    crew = make_crew(repo, tracker, host, test=UNRELATED_FAILURE)
    assert crew.run_once() == []
    assert tracker.calls == []          # nothing claimed, rejected or commented
    assert not (repo.parent / "agents.log").exists()
    logs = list((repo / ".cleaner-crew" / "runs").glob("*-baseline/tests.log"))
    assert logs and "alerts.test.ts" in logs[0].read_text()


def test_unrelated_failure_does_not_count_as_reproduction(repo):
    # Green on main, but once any test file is added an unrelated test fails, and the
    # output never mentions the new test.
    cmd = f"[ ! -f tests/test_bug.txt ] || {UNRELATED_FAILURE}"
    tracker, host = FakeTracker([task(21)]), FakeHost(TRUSTED)
    [outcome] = make_crew(repo, tracker, host, test=cmd).run_once()
    assert outcome.verdict == "escalate"
    assert "fails, but not in the new test" in outcome.reasons[0]
    assert [c["agent"] for c in agents_called(repo)] == ["manager", "inspector"]


def test_targeted_repro_must_fail_before_and_pass_after_the_fix(repo):
    targeted = "grep -q 'fixed = True' src/app.py && ls {files}"
    tracker, host = FakeTracker([task(22)]), FakeHost(TRUSTED)
    [outcome] = make_crew(repo, tracker, host, test_targeted=targeted).run_once()
    assert outcome.verdict == "mr", outcome.reasons


def test_repro_still_failing_after_fix_escalates(repo, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "nofix")
    targeted = "grep -q 'fixed = True' src/app.py && ls {files}"
    tracker, host = FakeTracker([task(23)]), FakeHost(TRUSTED)
    [outcome] = make_crew(repo, tracker, host, test_targeted=targeted).run_once()
    assert outcome.verdict == "escalate"
    assert outcome.reasons == ["the reproduction test still fails after the fix"]
    assert host.mrs == []


def test_agents_may_run_targeted_and_extra_test_commands(repo):
    crew = make_crew(repo, FakeTracker([]), FakeHost(), test_targeted="npx vitest run {files}",
                     agent_commands=["npm run test:unit"])
    tools = role_tools("janitor", crew.cfg)
    assert {"Bash(npx vitest run *)", "Bash(npm run test:unit)"} <= set(tools)
    assert not any("vitest" in t for t in role_tools("hooded", crew.cfg))


CHA_1 = ("htmlToText throws RangeError on out-of-range numeric entities, which can make a "
         "whole board poll fail")
CHA_2 = "htmlToText throws RangeError on out-of-range numeric entities"


@pytest.mark.parametrize("a,b,same", [
    (CHA_1, CHA_2, True),
    ("Fix off-by-one in pager", "fix off by one in pager!", True),
    ("Handle empty input in parser", "Handle empty input in renderer", False),
    ("Upgrade left-pad from 1.0.0 to 1.0.1", "Upgrade left-pad from 1.0.0 to 1.0.2", True),
    ("Fix typo", "Fix typo in README about the install command", False),  # too short
])
def test_similar_titles(a, b, same):
    assert similar_titles(a, b) is same


def test_scout_skips_reworded_duplicates_and_knows_existing_issues(repo, monkeypatch):
    monkeypatch.setenv("FAKE_SCOUT_TITLE", CHA_2)
    tracker, host = FakeTracker([]), FakeHost(TRUSTED)
    existing = Task("1", "CHA-1", CHA_1, "", "", labels=["cleaner-crew:rejected"])
    tracker.filed.append(existing)
    make_crew(repo, tracker, host, scout=True).run_once()

    assert tracker.filed == [existing]  # nothing new filed
    [scout] = agents_called(repo)
    assert CHA_1 in scout["prompt"] and "<known_issues>" in scout["prompt"]
