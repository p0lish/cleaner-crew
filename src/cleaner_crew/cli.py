from __future__ import annotations

import shutil
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from .config import Config, load_secrets
from .gitutil import git
from .policy import Policy

app = typer.Typer(help="Cleaner Crew: autonomous agents for small, safe fixes.",
                  no_args_is_help=True)
console = Console()


def _root() -> Path:
    try:
        return Path(git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    except RuntimeError:
        console.print("[red]not inside a git repository[/]")
        raise typer.Exit(1) from None


def _load() -> tuple[Config, Policy]:
    root = _root()
    load_secrets(root)
    try:
        return Config.load(root), Policy.load(root)
    except FileNotFoundError:
        console.print("[red]no .cleaner-crew/config.yml; run `cleaner-crew init` first[/]")
        raise typer.Exit(1) from None


@app.command()
def init() -> None:
    """Install the crew into this repository and set up connections."""
    from .installer import init as run_init
    run_init(_root())


@app.command()
def doctor() -> None:
    """Re-check connections, tools and the test baseline."""
    from .adapters import make_code_host, make_tracker
    from .detect import run_command

    cfg, policy = _load()
    table = Table("check", "status", "detail")

    def row(name: str, ok: bool, detail: str = "") -> None:
        table.add_row(name, "[green]ok[/]" if ok else "[red]fail[/]", detail)

    row("claude CLI", bool(shutil.which("claude")), shutil.which("claude") or "not on PATH")
    for name, make in (("code host", make_code_host), ("tracker", make_tracker)):
        try:
            r = make(cfg).check()
            row(name, r.ok, f"{r.service} {r.identity} {r.detail} "
                            + (f"missing: {', '.join(r.missing)}" if r.missing else ""))
        except Exception as e:  # noqa: BLE001
            row(name, False, str(e))
    try:
        from .gitutil import default_branch
        protected, detail = make_code_host(cfg).branch_protection(default_branch(cfg.root))
        row("branch protection", bool(protected),
            detail + ("" if protected else " — see README: Protecting the target repo"))
    except Exception as e:  # noqa: BLE001
        row("branch protection", False, str(e))
    res = run_command(cfg.commands.test, cfg.root, cfg.commands.test_timeout_s)
    row("test baseline", res.ok, cfg.commands.test)
    enabled = [c for c, r in policy.categories.items() if r.enabled]
    row("policy", bool(enabled), f"categories: {', '.join(enabled) or 'none enabled'}")
    row("kill switch", not cfg.stop_file.exists() and cfg.run.enabled,
        "STOP file present" if cfg.stop_file.exists() else
        ("run.enabled: false" if not cfg.run.enabled else "armed"))
    console.print(table)


@app.command()
def run(dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; claim/push nothing."),
        no_scout: bool = typer.Option(False, "--no-scout", help="Skip codebase scouting."),
        ) -> None:
    """Run the crew once: pick up to max_tasks_per_run tickets and resolve them."""
    from .adapters import make_code_host, make_tracker
    from .orchestrator import Crew

    cfg, policy = _load()
    if no_scout:
        cfg.run.scout_enabled = False
    crew = Crew(cfg, policy, make_tracker(cfg), make_code_host(cfg), dry_run=dry_run)
    outcomes = crew.run_once()
    if outcomes:
        table = Table("task", "verdict", "MR", "cost")
        for o in outcomes:
            table.add_row(o.task, o.verdict, o.mr_url or "-", f"${o.cost_usd:.2f}")
        console.print(table)


@app.command(name="trust")
def trust_cmd() -> None:
    """Show each category's earned trust level from recent MR history."""
    from . import BRANCH_PREFIX, trust
    from .adapters import make_code_host
    from .models import Category

    cfg, policy = _load()
    history = make_code_host(cfg).crew_mr_history(
        BRANCH_PREFIX, limit=policy.trust.window * len(Category))
    table = Table("category", "enabled", "level", "max", "samples", "accepted", "why")
    for cat, st in trust.compute(policy, history).items():
        rule = policy.rule(cat)
        table.add_row(cat.value, "yes" if rule.enabled else "no", st.level.value,
                      rule.max_level.value, str(st.samples),
                      f"{st.rate:.0%}" if st.rate is not None else "-", st.reason)
    console.print(table)


@app.command()
def verify(base: str = typer.Option(..., help="Target branch of the MR, e.g. main.")) -> None:
    """CI check for crew MRs: re-apply the base branch's policy to this branch's diff."""
    from .verify import verify as run_verify

    res = run_verify(_root(), base)
    for r in res.reasons:
        console.print(f"  - {r}")
    if not res.ok:
        console.print("[red]cleaner-crew verify: FAILED[/]")
        raise typer.Exit(1)
    console.print(f"[green]cleaner-crew verify: ok[/] ({res.category.value})")


@app.command()
def daemon(interval: int = typer.Option(0, help="Seconds between runs (default: config).")) -> None:
    """Run the crew in a loop on this machine."""
    cfg, _ = _load()
    every = interval or cfg.run.daemon_interval_s
    while True:
        try:
            run(dry_run=False, no_scout=False)
        except Exception as e:  # noqa: BLE001 - keep the daemon alive
            console.print(f"[red]run failed:[/] {e}")
        console.print(f"[dim]sleeping {every}s[/]")
        time.sleep(every)


@app.command()
def stop() -> None:
    """Kill switch: prevent any further runs until `resume`."""
    cfg, _ = _load()
    cfg.stop_file.touch()
    console.print(f"stopped ({cfg.stop_file}); commit it to stop CI runs too")


@app.command()
def resume() -> None:
    """Remove the kill switch."""
    cfg, _ = _load()
    cfg.stop_file.unlink(missing_ok=True)
    console.print("resumed")


if __name__ == "__main__":
    app()
