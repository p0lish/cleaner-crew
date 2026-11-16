"""Untrusted runs (tests, lint, mutants over agent-written code): no secrets, no network."""

import socket
import sys
from pathlib import Path

import pytest

from cleaner_crew import sandbox
from cleaner_crew.config import CodeHostConfig, CommandsConfig, Config, TrackerConfig, load_secrets
from cleaner_crew.detect import run_command

needs_netns = pytest.mark.skipif(not sandbox.isolation_available(),
                                 reason="no unprivileged network namespaces here")


def _cfg(root: Path) -> Config:
    return Config(root=root,
                  tracker=TrackerConfig("jira", "ENG", token_env="MY_JIRA", email_env="MY_MAIL"),
                  code_host=CodeHostConfig("gitlab", "a/b", "", "MY_GITLAB"),
                  commands=CommandsConfig(test="true"))


def test_scrub_removes_only_named_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_TOKEN", "s3cret")
    monkeypatch.setenv("HARMLESS", "fine")
    res = run_command('echo "[${CREW_TOKEN:-}] [$HARMLESS] [$CI]"', tmp_path, 30,
                      scrub={"CREW_TOKEN"})
    assert res.ok and res.output.strip() == "[] [fine] [1]"


def test_secret_names_cover_config_defaults_and_secrets_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "LOADED_SECRET_NAMES", set())
    import cleaner_crew.config as config_mod
    monkeypatch.setattr(config_mod, "LOADED_SECRET_NAMES", sandbox.LOADED_SECRET_NAMES)
    crew = tmp_path / ".cleaner-crew"
    crew.mkdir()
    (crew / "secrets.env").write_text("# comment\nNPM_TOKEN=abc\n")
    monkeypatch.delenv("NPM_TOKEN", raising=False)
    load_secrets(tmp_path)

    names = sandbox.secret_env_names(_cfg(tmp_path))
    assert {"MY_JIRA", "MY_MAIL", "MY_GITLAB", "NPM_TOKEN", "GITHUB_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"} <= names
    assert "" not in names


def test_isolation_fails_closed_when_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "_unshare_flags", lambda: None)
    marker = tmp_path / "ran"
    res = run_command(f"touch {marker}", tmp_path, 30, isolate_network=True)
    assert not res.ok and "isolate_network" in res.output
    assert not marker.exists(), "the command ran with network access anyway"


@needs_netns
def test_isolated_run_has_loopback_but_no_way_out(tmp_path):
    # A listener on the host's loopback stands in for "the internet": it's reachable
    # normally and must be unreachable from inside the namespace.
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        port = srv.getsockname()[1]
        dial = (f"{sys.executable} -c \"import socket; "
                f"socket.create_connection(('127.0.0.1', {port}), timeout=3)\"")
        assert run_command(dial, tmp_path, 30).ok
        assert not run_command(dial, tmp_path, 30, isolate_network=True).ok

    # ...while a test's own local server still works
    local = (f"{sys.executable} -c \"import socket; s = socket.socket(); "
             "s.bind(('127.0.0.1', 0)); s.listen(); "
             "socket.create_connection(s.getsockname(), timeout=3)\"")
    assert run_command(local, tmp_path, 30, isolate_network=True).ok


@needs_netns
def test_isolated_run_keeps_shell_semantics_and_exit_code(tmp_path):
    res = run_command("echo a | tr a b && exit 3", tmp_path, 30, env={"X": "1"},
                      isolate_network=True)
    assert not res.ok and res.output.strip() == "b"
    assert run_command('[ "$X" = 1 ]', tmp_path, 30, env={"X": "1"},
                       isolate_network=True).ok
