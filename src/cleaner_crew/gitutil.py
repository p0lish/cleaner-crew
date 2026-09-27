"""Git operations. Only the orchestrator runs these — agents never commit or push."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import DiffStats, FileChange


def git(*args: str, cwd: Path, check: bool = True) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout.strip()


@dataclass
class Remote:
    host: str  # github.com, gitlab.com, gitlab.acme.internal
    path: str  # owner/repo or group/sub/repo

    @property
    def kind(self) -> str:
        if self.host == "github.com" or self.host.endswith(".ghe.com"):
            return "github"
        if "gitlab" in self.host:
            return "gitlab"
        return "unknown"


_REMOTE_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<h1>[^/:]+)(?::\d+)?/|[^@]+@(?P<h2>[^:]+):)"
    r"(?P<path>.+?)(?:\.git)?/?$"
)


def parse_remote(url: str) -> Remote | None:
    m = _REMOTE_RE.match(url.strip())
    if not m:
        return None
    return Remote(host=m.group("h1") or m.group("h2"), path=m.group("path"))


def origin(root: Path) -> Remote | None:
    url = git("remote", "get-url", "origin", cwd=root, check=False)
    return parse_remote(url) if url else None


def default_branch(root: Path) -> str:
    ref = git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=root, check=False)
    return ref.removeprefix("origin/") if ref else "main"


def create_worktree(root: Path, branch: str, base: str) -> Path:
    git("fetch", "origin", base, cwd=root)
    path = root / ".cleaner-crew" / "worktrees" / branch.replace("/", "-")
    git("worktree", "add", "-B", branch, str(path), f"origin/{base}", cwd=root)
    return path


def remove_worktree(root: Path, path: Path) -> None:
    git("worktree", "remove", "--force", str(path), cwd=root, check=False)


def commit_all(wt: Path, message: str) -> bool:
    git("add", "-A", cwd=wt)
    if not git("status", "--porcelain", cwd=wt):
        return False
    git("-c", "user.name=Cleaner Crew", "-c", "user.email=cleaner-crew@localhost",
        "commit", "-q", "-m", message, cwd=wt)
    return True


def diff_stats(wt: Path, base: str) -> DiffStats:
    out = git("diff", "--numstat", f"origin/{base}...HEAD", cwd=wt)
    files = []
    for line in out.splitlines():
        added, removed, path = line.split("\t", 2)
        # binary files report "-"
        files.append(FileChange(path, int(added) if added != "-" else 0,
                                int(removed) if removed != "-" else 0))
    return DiffStats(files)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def changed_lines(wt: Path, base: str) -> dict[str, list[int]]:
    """Added/modified line numbers (in the new version) per file."""
    out: dict[str, list[int]] = {}
    path = None
    for line in git("diff", "-U0", "--no-color", f"origin/{base}...HEAD", cwd=wt).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := _HUNK_RE.match(line)):
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(path, []).extend(range(start, start + count))
    return {p: ls for p, ls in out.items() if ls}


def branch_commit_messages(wt: Path, base: str, head: str = "HEAD") -> list[str]:
    out = git("log", "--format=%B%x1e", f"origin/{base}..{head}", cwd=wt)
    return [m.strip() + "\n" for m in out.split("\x1e") if m.strip()]


def diff_text(wt: Path, base: str) -> str:
    return git("diff", f"origin/{base}...HEAD", cwd=wt)


def push(wt: Path, branch: str) -> None:
    git("push", "--force-with-lease", "-u", "origin", branch, cwd=wt)
