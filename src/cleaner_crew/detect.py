"""Detect the project's stack so the installer can propose commands."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Stack:
    name: str
    test: str
    lint: str = ""
    ci_install: str = ""  # reproducible install of the current lockfile (CI, fresh worktrees)
    install: str = ""  # upgrade one dependency: {package} and {version} are substituted
    outdated: str = ""  # machine-readable list of outdated dependencies
    post_install: list[str] = field(default_factory=list)  # e.g. download browsers


def _node_runner(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


# Install scripts are the main supply-chain attack vector; upgrades never run them.
NODE_COMMANDS = {
    "npm": ("npm ci", "npm install {package}@{version} --ignore-scripts",
            "npm outdated --json"),
    "pnpm": ("pnpm install --frozen-lockfile", "pnpm add {package}@{version} --ignore-scripts",
             "pnpm outdated --format json"),
    "yarn": ("yarn install --frozen-lockfile", "yarn add {package}@{version} --ignore-scripts",
             ""),
    "bun": ("bun install --frozen-lockfile", "bun add {package}@{version} --ignore-scripts", ""),
}


def detect_stack(root: Path) -> Stack | None:
    if (root / "package.json").exists():
        pkg = json.loads((root / "package.json").read_text())
        scripts = pkg.get("scripts", {})
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        run = _node_runner(root)
        ci_install, install, outdated = NODE_COMMANDS[run]
        post = []
        if "@playwright/test" in deps or "playwright" in deps:
            # No --with-deps: older Playwright versions apt-install package names that no
            # longer exist on newer Ubuntu; CI runner images already ship the libraries.
            post.append("npx playwright install chromium")
        return Stack(
            "node",
            test=f"{run} test" if "test" in scripts else "",
            lint=f"{run} run lint" if "lint" in scripts else "",
            ci_install=ci_install, install=install, outdated=outdated, post_install=post,
        )
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        text = (root / "pyproject.toml").read_text(errors="ignore") if (
            root / "pyproject.toml").exists() else ""
        if (root / "uv.lock").exists():
            return Stack("python", test="uv run pytest -q",
                         lint="uv run ruff check ." if "ruff" in text else "",
                         ci_install="uv sync --locked",
                         install="uv add {package}=={version}",
                         outdated="uv pip list --outdated --format json")
        prefix = "poetry run " if (root / "poetry.lock").exists() else ""
        return Stack("python", test=f"{prefix}pytest -q",
                     lint=f"{prefix}ruff check ." if "ruff" in text else "",
                     ci_install="poetry install" if prefix else "pip install -e .")
    if (root / "go.mod").exists():
        return Stack("go", test="go test ./...", lint="go vet ./...",
                     ci_install="go mod download", install="go get {package}@v{version}")
    if (root / "Cargo.toml").exists():
        return Stack("rust", test="cargo test", lint="cargo clippy -- -D warnings",
                     ci_install="cargo fetch")
    if (root / "Gemfile").exists():
        return Stack("ruby", test="bundle exec rspec" if (root / "spec").exists()
                     else "bundle exec rake test", ci_install="bundle install")
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


def run_command(cmd: str, cwd: Path, timeout_s: int, env: dict | None = None) -> CommandResult:
    # CI=1 and no stdin: test runners like vitest/jest otherwise start in watch mode when
    # the daemon runs in a terminal, and hang until the timeout.
    full_env = {**os.environ, "CI": "1", **(env or {})}
    try:
        res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout_s, stdin=subprocess.DEVNULL, env=full_env)
    except subprocess.TimeoutExpired:
        return CommandResult(False, f"timed out after {timeout_s}s")
    out = (res.stdout + res.stderr)[-8000:]
    return CommandResult(res.returncode == 0, out)
