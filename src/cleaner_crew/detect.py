"""Detect the project's stack so the installer can propose test/lint commands."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Stack:
    name: str
    test: str
    lint: str = ""


def _node_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def detect_stack(root: Path) -> Stack | None:
    if (root / "package.json").exists():
        scripts = json.loads((root / "package.json").read_text()).get("scripts", {})
        run = _node_runner(root)
        return Stack(
            "node",
            test=f"{run} test" if "test" in scripts else "",
            lint=f"{run} run lint" if "lint" in scripts else "",
        )
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        prefix = "uv run " if (root / "uv.lock").exists() else (
            "poetry run " if (root / "poetry.lock").exists() else "")
        lint = f"{prefix}ruff check ." if "ruff" in (root / "pyproject.toml").read_text(
            errors="ignore") else ""
        return Stack("python", test=f"{prefix}pytest -q", lint=lint)
    if (root / "go.mod").exists():
        return Stack("go", test="go test ./...", lint="go vet ./...")
    if (root / "Cargo.toml").exists():
        return Stack("rust", test="cargo test", lint="cargo clippy -- -D warnings")
    if (root / "Gemfile").exists():
        return Stack("ruby", test="bundle exec rspec" if (root / "spec").exists()
                     else "bundle exec rake test")
    if (root / "pom.xml").exists():
        return Stack("java-maven", test="mvn -q test")
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return Stack("java-gradle", test="./gradlew test")
    if (root / "Makefile").exists() and "\ntest:" in (root / "Makefile").read_text():
        return Stack("make", test="make test")
    return None


@dataclass
class CommandResult:
    ok: bool
    output: str


def run_command(cmd: str, cwd: Path, timeout_s: int) -> CommandResult:
    try:
        res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return CommandResult(False, f"timed out after {timeout_s}s")
    out = (res.stdout + res.stderr)[-8000:]
    return CommandResult(res.returncode == 0, out)
