"""PreToolUse hook injected into every agent run via `claude --settings`.

It enforces policy on each tool call, independent of what the agent's prompt says:
  * no file access outside the task's worktree, and never to secrets files
  * writes never touch forbidden paths
  * janitors may not edit tests, inspectors may only edit tests (no grading own homework)
  * no network, git history or privilege commands from Bash

Exit code 2 blocks the call and feeds stderr back to the agent.
Configured through env vars set by the orchestrator:
  CLEANER_CREW_ROLE, CLEANER_CREW_WORKTREE, CLEANER_CREW_ROOT, CLEANER_CREW_CATEGORY
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from ..models import Category
from ..policy import Policy, matches

WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}
READ_TOOLS = {"Read", "Grep", "Glob"}
NETWORK_TOOLS = {"WebFetch", "WebSearch"}
SECRET_GLOBS = ["**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/secrets.env",
                "**/.npmrc", "**/.pypirc", "**/.netrc", "**/credentials*"]
# The crew must never be able to rewrite its own rules, whatever policy.yml says.
ALWAYS_FORBIDDEN_WRITES = [".cleaner-crew/**", ".claude/**", ".git/**", ".mcp.json"]

BASH_DENY = [
    r"\bgit\s+(push|commit|reset|rebase|checkout|switch|remote|config|worktree|fetch|pull)\b",
    r"\b(curl|wget|ssh|scp|rsync|nc|ncat|telnet|ftp)\b",
    r"\b(sudo|su|doas|chmod\s+\+s|chown)\b",
    r"\b(env|printenv|set)\s*($|\|)",
    r"\$\{?[A-Z_]*(TOKEN|KEY|SECRET|PASSWORD)",
    r"(^|\s)(cat|less|head|tail|cp|mv)\s+[^|;&]*\.env\b",
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\.\.)",
    r"\bgh\s|\bglab\s",
]


def _deny(msg: str) -> None:
    print(f"cleaner-crew guard: {msg}", file=sys.stderr)
    sys.exit(2)


def _rel(path: str, worktree: Path) -> str | None:
    p = Path(path)
    p = (worktree / p) if not p.is_absolute() else p
    try:
        return p.resolve().relative_to(worktree.resolve()).as_posix()
    except ValueError:
        return None


def check(event: dict, role: str, worktree: Path, policy: Policy,
          category: Category | None) -> str | None:
    """Return a denial reason, or None if the call is allowed."""
    tool = event.get("tool_name", "")
    inp = event.get("tool_input") or {}

    if tool in NETWORK_TOOLS:
        return "network tools are disabled for the crew"

    if tool in WRITE_TOOLS | READ_TOOLS:
        raw = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or "."
        rel = _rel(raw, worktree)
        if rel is None:
            return f"{raw} is outside the task worktree"
        if matches(rel, SECRET_GLOBS):
            return f"{rel} may contain secrets"
        if tool in WRITE_TOOLS:
            if role in ("scout", "manager", "hooded"):
                return f"the {role} role is read-only"
            if matches(rel, ALWAYS_FORBIDDEN_WRITES):
                return f"{rel} is crew configuration and cannot be edited by agents"
            if policy.is_forbidden(rel, category):
                return f"{rel} is a forbidden path in policy.yml"
            is_test = policy.is_test(rel)
            if role == "janitor" and is_test:
                return f"{rel} is a test; tests are the inspector's job"
            if role == "inspector" and not is_test:
                return f"{rel} is not a test file; inspectors only write tests"

    if tool == "Bash":
        cmd = inp.get("command", "")
        for pat in BASH_DENY:
            if re.search(pat, cmd):
                return f"command not allowed for the crew: {cmd[:120]}"

    return None


def main() -> None:
    event = json.load(sys.stdin)
    role = os.environ.get("CLEANER_CREW_ROLE", "")
    worktree = Path(os.environ.get("CLEANER_CREW_WORKTREE") or event.get("cwd") or ".")
    root = Path(os.environ.get("CLEANER_CREW_ROOT", worktree))
    cat = os.environ.get("CLEANER_CREW_CATEGORY")
    try:
        policy = Policy.load(root)
    except FileNotFoundError:
        _deny("policy.yml missing; refusing all tool calls")
    reason = check(event, role, worktree, policy, Category(cat) if cat else None)
    if reason:
        _deny(reason)


if __name__ == "__main__":
    main()
