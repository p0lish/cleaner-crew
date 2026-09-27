"""`cleaner-crew init`: install the crew into a cloned repository.

Walks through: code host detection + auth check -> tracker choice + auth check ->
workflow mapping -> stack/test detection + green baseline -> writing files -> runner setup.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib import resources
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.table import Table

from . import CREW_DIR
from .adapters import make_code_host
from .config import (CodeHostConfig, CommandsConfig, Config, RunConfig, TrackerConfig,
                     load_secrets)
from .detect import detect_stack, run_command
from .gitutil import origin
from .models import ConnectionReport

console = Console()

TOKEN_HELP = {
    "github": "https://github.com/settings/personal-access-tokens (fine-grained: "
              "Contents + Pull requests read/write on this repo), or run `gh auth login`",
    "gitlab": "Project > Settings > Access tokens (role Developer, scopes api, write_repository)",
    "linear": "https://linear.app/settings/account/security -> Personal API keys",
    "jira": "https://id.atlassian.com/manage-profile/security/api-tokens",
}

# STOP is deliberately not ignored: committing it halts CI runs too.
GITIGNORE = "secrets.env\nruns/\nworktrees/\nrun.lock\n"


# -- small helpers -------------------------------------------------------------

def _template(*parts: str) -> str:
    node = resources.files("cleaner_crew.templates")
    for p in parts:
        node = node / p
    return node.read_text()


def _write(path: Path, content: str, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        console.print(f"  [dim]kept existing {path}[/]")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    console.print(f"  [green]wrote[/] {path}")


def _save_secret(root: Path, name: str, value: str) -> None:
    path = root / CREW_DIR / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [l for l in (path.read_text().splitlines() if path.exists() else [])
             if not l.startswith(f"{name}=")]
    lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    os.environ[name] = value


def _ensure_secret(root: Path, name: str, service: str, secret: bool = True) -> None:
    if os.environ.get(name):
        console.print(f"  found [bold]{name}[/] in environment")
        return
    console.print(f"  [yellow]{name} not set.[/] Create one at: {TOKEN_HELP.get(service, '')}")
    value = Prompt.ask(f"  paste {name}", password=secret)
    _save_secret(root, name, value.strip())
    console.print(f"  saved to {CREW_DIR}/secrets.env (gitignored, mode 600)")


def _report(r: ConnectionReport) -> bool:
    mark = "[green]✓[/]" if r.ok else "[red]✗[/]"
    console.print(f"  {mark} {r.service}: {r.identity or '-'} {r.detail}")
    for m in r.missing:
        console.print(f"    [red]missing:[/] {m}")
    return r.ok


def _pick(label: str, options: list[str], guesses: list[str]) -> str:
    default = next((o for g in guesses for o in options if o.lower() == g.lower()), options[0])
    return Prompt.ask(f"  {label}", choices=options, default=default)


# -- steps ---------------------------------------------------------------------

def setup_code_host(root: Path) -> CodeHostConfig:
    console.rule("[bold]1. Code host")
    remote = origin(root)
    if remote is None:
        console.print("[red]No `origin` remote. Run this inside a cloned repository.[/]")
        raise SystemExit(1)
    kind = remote.kind
    if kind == "unknown":
        kind = Prompt.ask(f"  Could not tell what {remote.host} is", choices=["github", "gitlab"])
    console.print(f"  origin: [bold]{kind}[/] {remote.host}/{remote.path}")

    if kind == "github":
        api = ("https://api.github.com" if remote.host == "github.com"
               else f"https://{remote.host}/api/v3")
        cfg = CodeHostConfig("github", remote.path, api, "GITHUB_TOKEN")
    else:
        cfg = CodeHostConfig("gitlab", remote.path, f"https://{remote.host}/api/v4",
                             "GITLAB_TOKEN")

    while True:
        try:
            host = make_code_host(_shell_cfg(root, code_host=cfg))
            if _report(host.check()):
                return cfg
        except RuntimeError as e:
            console.print(f"  [red]{e}[/]")
        _ensure_secret(root, cfg.token_env, kind)
        if not Confirm.ask("  retry connection check?", default=True):
            raise SystemExit(1)


def setup_tracker(root: Path) -> TrackerConfig:
    console.rule("[bold]2. Task manager")
    detected = []
    if os.environ.get("LINEAR_API_KEY"):
        detected.append("linear")
    if os.environ.get("JIRA_API_TOKEN"):
        detected.append("jira")
    if detected:
        console.print(f"  detected credentials for: {', '.join(detected)}")
    kind = Prompt.ask("  which task manager?", choices=["linear", "jira"],
                      default=detected[0] if detected else "linear")

    if kind == "linear":
        cfg = TrackerConfig("linear", project="", token_env="LINEAR_API_KEY")
        _ensure_secret(root, cfg.token_env, "linear")
    else:
        base = os.environ.get("JIRA_BASE_URL") or Prompt.ask(
            "  Jira site URL", default="https://your-company.atlassian.net")
        cfg = TrackerConfig("jira", project="", base_url=base.rstrip("/"),
                            token_env="JIRA_API_TOKEN", email_env="JIRA_EMAIL",
                            status_todo="To Do")
        _ensure_secret(root, cfg.email_env, "jira", secret=False)
        _ensure_secret(root, cfg.token_env, "jira")

    from .adapters import make_tracker
    tracker = make_tracker(_shell_cfg(root, tracker=cfg))
    try:
        projects = tracker.list_projects()
    except Exception as e:  # noqa: BLE001
        console.print(f"  [red]could not connect: {e}[/]")
        raise SystemExit(1) from e

    table = Table("key", "name", title="Projects" if kind == "jira" else "Teams")
    for k, n in projects:
        table.add_row(k, n)
    console.print(table)
    cfg.project = Prompt.ask("  which one should the crew work in?",
                             choices=[k for k, _ in projects])

    tracker = make_tracker(_shell_cfg(root, tracker=cfg))
    if not _report(tracker.check()):
        raise SystemExit(1)

    statuses = tracker.list_statuses()
    console.print(f"  statuses: {', '.join(statuses)}")
    cfg.status_todo = _pick("status for 'todo'", statuses, ["Todo", "To Do", "Backlog", "Open"])
    cfg.status_in_progress = _pick("status for 'in progress'", statuses,
                                   ["In Progress", "Doing", "Started"])
    cfg.status_in_review = _pick("status for 'in review'", statuses,
                                 ["In Review", "Code Review", "Review", "In Progress"])
    cfg.candidate_label = Prompt.ask("  label that marks tickets as crew candidates",
                                     default=cfg.candidate_label)
    return cfg


def setup_commands(root: Path) -> tuple[CommandsConfig, bool]:
    console.rule("[bold]3. Tests")
    stack = detect_stack(root)
    if stack:
        console.print(f"  detected stack: [bold]{stack.name}[/]")
    test = Prompt.ask("  test command", default=stack.test if stack and stack.test else None)
    lint = Prompt.ask("  lint command (empty for none)",
                      default=stack.lint if stack else "", show_default=True)
    cmds = CommandsConfig(test=test, lint=lint or "")

    console.print("  running the test suite once to check the baseline is green...")
    res = run_command(cmds.test, root, cmds.test_timeout_s)
    if res.ok:
        console.print("  [green]✓ baseline green[/]")
        return cmds, True
    console.print(f"[red]  ✗ baseline failing[/]\n{res.output[-1500:]}")
    console.print("  [yellow]The crew relies on a green baseline to tell its own breakage from "
                  "existing failures. It will be installed disabled (run.enabled: false).[/]")
    return cmds, False


def setup_runner(root: Path, cfg: Config) -> None:
    console.rule("[bold]5. Where should it run?")
    mode = Prompt.ask("  runner", choices=["ci", "local", "both", "none"], default="both")
    spec = "cleaner-crew"
    if mode in ("ci", "both"):
        spec = Prompt.ask("  package spec for CI (PyPI name or git+https URL)", default=spec)
        if cfg.code_host.kind == "github":
            tracker_env = ("LINEAR_API_KEY: ${{ secrets.LINEAR_API_KEY }}"
                           if cfg.tracker.kind == "linear" else
                           "JIRA_EMAIL: ${{ secrets.JIRA_EMAIL }}\n"
                           "          JIRA_API_TOKEN: ${{ secrets.JIRA_API_TOKEN }}")
            wf = (_template("ci", "github.yml").replace("__TRACKER_ENV__", tracker_env)
                  .replace("__PACKAGE_SPEC__", spec))
            _write(root / ".github" / "workflows" / "cleaner-crew.yml", wf)
            secrets = ["ANTHROPIC_API_KEY"] + (["LINEAR_API_KEY"] if cfg.tracker.kind == "linear"
                                               else ["JIRA_EMAIL", "JIRA_API_TOKEN"])
            console.print("  add repository secrets: " + ", ".join(secrets))
            console.print("  (e.g. `gh secret set ANTHROPIC_API_KEY`)")
        else:
            _write(root / CREW_DIR / "ci" / "gitlab.yml",
                   _template("ci", "gitlab.yml").replace("__PACKAGE_SPEC__", spec))
            console.print("  add `include: { local: .cleaner-crew/ci/gitlab.yml }` to "
                          ".gitlab-ci.yml and create a pipeline schedule with CLEANER_CREW=1")

    if mode in ("local", "both"):
        exe = shutil.which("cleaner-crew") or f"{sys.executable} -m cleaner_crew"
        subs = {"__REPO_ROOT__": str(root), "__REPO_NAME__": root.name, "__EXECUTABLE__": exe,
                "__INTERVAL__": str(cfg.run.daemon_interval_s)}
        unit_dir = root / CREW_DIR / "systemd"
        for name in ("cleaner-crew.service", "cleaner-crew.timer"):
            text = _template("systemd", name)
            for k, v in subs.items():
                text = text.replace(k, v)
            _write(unit_dir / name, text, overwrite=True)
        console.print(
            "  local options:\n"
            f"    • foreground loop:  cleaner-crew daemon\n"
            f"    • systemd timer:    ln -s {unit_dir}/cleaner-crew.{{service,timer}} "
            "~/.config/systemd/user/ && systemctl --user enable --now cleaner-crew.timer")


def _shell_cfg(root: Path, tracker: TrackerConfig | None = None,
               code_host: CodeHostConfig | None = None) -> Config:
    """A partial config, just enough to build one adapter during setup."""
    return Config(root=root,
                  tracker=tracker or TrackerConfig("linear", ""),
                  code_host=code_host or CodeHostConfig("github", "", "", ""),
                  commands=CommandsConfig(test=""))


def init(root: Path) -> None:
    console.print("[bold]Cleaner Crew setup[/] :broom:\n")
    load_secrets(root)
    if (root / CREW_DIR / "config.yml").exists() and not Confirm.ask(
            "  existing config found; reconfigure?", default=False):
        return

    code_host = setup_code_host(root)
    tracker = setup_tracker(root)
    commands, green = setup_commands(root)
    cfg = Config(root=root, tracker=tracker, code_host=code_host, commands=commands,
                 run=RunConfig(enabled=green))

    console.rule("[bold]4. Writing files")
    _write(root / CREW_DIR / "config.yml", cfg.dump(), overwrite=True)
    _write(root / CREW_DIR / "policy.yml", _template("policy.yml"))
    _write(root / CREW_DIR / ".gitignore", GITIGNORE, overwrite=True)
    for role in ("scout", "manager", "janitor", "inspector", "hooded"):
        _write(root / ".claude" / "agents" / f"cleaner-crew-{role}.md",
               _template("agents", f"{role}.md"))

    setup_runner(root, cfg)

    console.rule("[bold]Done")
    console.print(
        "Next:\n"
        f"  1. review {CREW_DIR}/policy.yml (what the crew is allowed to ship)\n"
        f"  2. label a couple of tickets '{tracker.candidate_label}'\n"
        "  3. cleaner-crew run --dry-run    (plans only; nothing is claimed or pushed)\n"
        "  4. commit .cleaner-crew/ and .claude/agents/ so CI and teammates share the setup\n"
        f"Kill switch: touch {CREW_DIR}/STOP  (or set run.enabled: false)")
