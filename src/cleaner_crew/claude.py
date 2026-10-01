"""Run one crew role as a headless Claude Code session.

Every role runs in a fresh context (separate `claude -p` process) so the inspector and
the hooded agent never see the janitor's reasoning, only its output.

Layers that restrict what an agent can do:
  1. `--tools`: only the tools in the agent definition's `tools:` frontmatter exist
  2. `--allowedTools` / `--disallowedTools` per role (e.g. which Bash commands)
  3. `--restricted`: ignores the target repo's own .claude settings files and confines
     file tools to the task worktree
  4. the PreToolUse guard hook (cleaner_crew.hooks.guard), passed via `--settings`

The role's instructions are appended to the system prompt rather than selected with
`--agent`, because Claude Code ignores `--json-schema` when `--agent` is set.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from .config import Config

ROLES = ("scout", "manager", "janitor", "quartermaster", "inspector", "hooded")
READ_ONLY = ["Read", "Grep", "Glob"]
ALWAYS_DENIED = ["WebFetch", "WebSearch", "Task", "Agent"]


def _cmd_rules(cmd: str) -> list[str]:
    return [f"Bash({cmd})", f"Bash({cmd} *)"] if cmd else []


def role_tools(role: str, cfg: Config) -> list[str]:
    c = cfg.commands
    targeted = c.test_targeted.split("{files}")[0].strip()
    run_checks = [rule for cmd in (c.test, c.lint, targeted, *c.agent_commands)
                  for rule in _cmd_rules(cmd)]
    return {
        "scout": READ_ONLY + ["Bash(git log *)", "Bash(git ls-files *)"],
        "manager": READ_ONLY,
        "janitor": READ_ONLY + ["Edit", "Write"] + run_checks,
        "quartermaster": READ_ONLY + ["Edit", "Write"] + run_checks,
        "inspector": READ_ONLY + ["Edit", "Write"] + run_checks,
        "hooded": READ_ONLY + ["Bash(git diff *)", "Bash(git log *)"],
    }[role]


def load_agent_definition(role: str, root: Path) -> dict:
    """Prefer the repo's customised copy in .claude/agents, fall back to the bundled template."""
    local = root / ".claude" / "agents" / f"cleaner-crew-{role}.md"
    text = local.read_text() if local.exists() else (
        resources.files("cleaner_crew.templates.agents") / f"{role}.md").read_text()
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front)
    # Frontmatter allows "Read, Grep"; the --agents JSON requires a list.
    tools = meta.get("tools", READ_ONLY)
    if isinstance(tools, str):
        tools = [t.strip() for t in tools.split(",") if t.strip()]
    return {"description": meta["description"], "prompt": body.strip(), "tools": tools}


@dataclass
class AgentResult:
    ok: bool
    output: dict | None
    text: str
    cost_usd: float = 0.0
    denials: list = field(default_factory=list)


def guard_settings() -> str:
    cmd = f"{shlex.quote(sys.executable)} -m cleaner_crew.hooks.guard"
    return json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}]}})


def run_agent(role: str, prompt: str, *, cfg: Config, cwd: Path, schema: dict,
              budget_usd: float, audit_dir: Path, category: str | None = None,
              timeout_s: int = 1800) -> AgentResult:
    agent = load_agent_definition(role, cfg.root)
    argv = [
        "claude", "-p", prompt,
        "--append-system-prompt", agent["prompt"],
        "--restricted",
        "--tools", ",".join(agent["tools"]),
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--allowedTools", *role_tools(role, cfg),
        "--disallowedTools", *ALWAYS_DENIED,
        "--max-budget-usd", f"{max(budget_usd, 0.01):.2f}",
        "--settings", guard_settings(),
        "--strict-mcp-config",  # no MCP servers: agents get no tracker/code host access
        "--no-session-persistence",
    ]
    if cfg.run.model:
        argv += ["--model", cfg.run.model]

    env = {**os.environ,
           "CLEANER_CREW_ROLE": role,
           "CLEANER_CREW_WORKTREE": str(cwd),
           "CLEANER_CREW_ROOT": str(cfg.root)}
    if category:
        env["CLEANER_CREW_CATEGORY"] = category
    # Agents never need tracker or code host credentials.
    for var in (cfg.tracker.token_env, cfg.tracker.email_env, cfg.code_host.token_env,
                "GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN"):
        if var:
            env.pop(var, None)

    audit_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return AgentResult(False, None, f"{role} timed out after {timeout_s}s")
    (audit_dir / f"{role}.json").write_text(proc.stdout or proc.stderr)

    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return AgentResult(False, None, (proc.stderr or proc.stdout)[-2000:])

    output = res.get("structured_output")
    if output is None:
        output = _json_from_text(res.get("result") or "")
    return AgentResult(
        ok=not res.get("is_error") and output is not None,
        output=output,
        text=res.get("result", ""),
        cost_usd=float(res.get("total_cost_usd") or 0),
        denials=res.get("permission_denials", []),
    )


_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def _json_from_text(text: str) -> dict | None:
    """Fallback when structured output is missing: a bare or fenced JSON object."""
    candidates = [text.strip()] + [m.group(1) for m in _FENCED_JSON.finditer(text)][::-1]
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
