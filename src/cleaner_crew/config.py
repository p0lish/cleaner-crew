"""Loading `.cleaner-crew/config.yml` and `.cleaner-crew/policy.yml`.

Config holds *where* things are (tracker, code host, commands). Secrets never live
here — only the names of the environment variables that hold them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import CREW_DIR


@dataclass
class TrackerConfig:
    kind: str  # "linear" | "jira"
    # Linear: team key. Jira: project key.
    project: str
    base_url: str = ""  # Jira only, e.g. https://acme.atlassian.net
    candidate_label: str = "cleaner-crew"  # a human applied this: the crew may work on it
    proposed_label: str = "cleaner-crew:proposed"  # scout finding awaiting human promotion
    shadow_label: str = "cleaner-crew:shadow"  # planned in shadow mode, left for a human
    in_progress_label: str = "cleaner-crew:in-progress"
    rejected_label: str = "cleaner-crew:rejected"
    escalated_label: str = "cleaner-crew:needs-human"
    status_todo: str = "Todo"
    status_in_progress: str = "In Progress"
    status_in_review: str = "In Review"
    token_env: str = ""
    email_env: str = ""  # Jira only


@dataclass
class CodeHostConfig:
    kind: str  # "github" | "gitlab"
    repo: str  # owner/name or group/subgroup/name
    api_url: str
    token_env: str
    mr_label: str = "cleaner-crew"


@dataclass
class CommandsConfig:
    test: str
    lint: str = ""
    test_timeout_s: int = 900
    ci_install: str = ""  # installs the locked dependencies (CI, fresh worktrees)
    install: str = ""  # upgrades one dependency; {package} and {version} are substituted
    outdated: str = ""  # lists outdated dependencies as JSON (npm/pnpm/uv formats)
    post_install: list[str] = field(default_factory=list)  # e.g. npx playwright install


@dataclass
class RunConfig:
    enabled: bool = True
    max_tasks_per_run: int = 1
    scout_enabled: bool = True
    scout_when_fewer_than: int = 2
    max_findings_per_scout: int = 3
    model: str = ""  # empty -> Claude Code default
    daemon_interval_s: int = 3600


@dataclass
class Config:
    root: Path
    tracker: TrackerConfig
    code_host: CodeHostConfig
    commands: CommandsConfig
    run: RunConfig = field(default_factory=RunConfig)

    @property
    def crew_dir(self) -> Path:
        return self.root / CREW_DIR

    @property
    def stop_file(self) -> Path:
        """Kill switch: touch this file and every run exits immediately."""
        return self.crew_dir / "STOP"

    @classmethod
    def load(cls, root: Path) -> Config:
        data = yaml.safe_load((root / CREW_DIR / "config.yml").read_text())
        return cls(
            root=root,
            tracker=TrackerConfig(**data["tracker"]),
            code_host=CodeHostConfig(**data["code_host"]),
            commands=CommandsConfig(**data["commands"]),
            run=RunConfig(**data.get("run", {})),
        )

    def dump(self) -> str:
        from dataclasses import asdict

        d = {
            "tracker": asdict(self.tracker),
            "code_host": asdict(self.code_host),
            "commands": asdict(self.commands),
            "run": asdict(self.run),
        }
        return yaml.safe_dump(d, sort_keys=False)


def load_secrets(root: Path) -> None:
    """Load `.cleaner-crew/secrets.env` (gitignored, 0600) into the environment.

    Existing environment variables win, so CI secrets override local files.
    """
    path = root / CREW_DIR / "secrets.env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def env(name: str) -> str:
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(f"environment variable {name} is not set")
    return val
