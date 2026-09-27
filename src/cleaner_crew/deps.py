"""Dependency upgrades: semver, outdated reports, lockfile inspection, release notes.

Everything here is deterministic and runs in the orchestrator. Agents never get network
access; the "supply run" (installing the new version) happens here, with install
scripts disabled, and agents only see its results.
"""

from __future__ import annotations

import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_SEMVER = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?P<pre>[-+].*)?$")


def parse_version(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match((v or "").strip())
    if not m or m.group("pre"):
        return None  # prereleases and non-semver versions are never auto-upgraded
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


def bump_kind(current: str, target: str) -> str:
    """patch | minor | major | none | unknown. In 0.x, a minor bump counts as major."""
    a, b = parse_version(current), parse_version(target)
    if a is None or b is None:
        return "unknown"
    if b <= a:
        return "none"
    if b[0] != a[0] or (a[0] == 0 and b[1] != a[1]):
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


@dataclass
class Outdated:
    package: str
    current: str
    wanted: str
    latest: str

    def target(self, allowed: list[str]) -> str | None:
        """Newest version whose bump kind is allowed by policy."""
        for candidate in (self.latest, self.wanted):
            if candidate and bump_kind(self.current, candidate) in allowed:
                return candidate
        return None


def parse_outdated(text: str) -> list[Outdated]:
    """npm/pnpm (`{name: {current, wanted, latest}}`) or uv/pip (`[{name, version, ...}]`)."""
    try:
        data = json.loads(text or "null")
    except json.JSONDecodeError:
        return []
    out = []
    if isinstance(data, dict):
        for name, info in data.items():
            if isinstance(info, list):  # npm reports one entry per workspace
                info = info[0] if info else {}
            if info.get("current"):
                out.append(Outdated(name, info["current"], info.get("wanted", ""),
                                    info.get("latest", "")))
    elif isinstance(data, list):
        for info in data:
            if info.get("name") and info.get("version"):
                out.append(Outdated(info["name"], info["version"], "",
                                    info.get("latest_version", "")))
    return out


def outdated(cmd: str, cwd: Path, timeout_s: int = 300) -> list[Outdated]:
    # Not detect.run_command: that truncates output, and `npm outdated` exits 1 when
    # anything is outdated.
    try:
        res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout_s, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return []
    return parse_outdated(res.stdout)


# -- lockfiles ---------------------------------------------------------------------

LOCKFILES = ("package-lock.json", "uv.lock")


@dataclass
class LockPackage:
    name: str
    version: str
    install_script: bool = False


def read_lock(filename: str, text: str) -> dict[str, LockPackage]:
    """Key -> package. Keys are install paths for npm (a package can appear nested)."""
    if not text:
        return {}
    if filename.endswith("package-lock.json"):
        pkgs = json.loads(text).get("packages", {})
        return {path: LockPackage(path.rsplit("node_modules/", 1)[-1], info.get("version", ""),
                                  bool(info.get("hasInstallScript")))
                for path, info in pkgs.items() if path.startswith("node_modules/")}
    if filename.endswith("uv.lock"):
        return {p["name"]: LockPackage(p["name"], p.get("version", ""))
                for p in tomllib.loads(text).get("package", [])}
    return {}


def locked_version(root: Path, package: str) -> str | None:
    """Currently locked top-level version of `package`, if we can read the lockfile."""
    for filename in LOCKFILES:
        path = root / filename
        if path.exists():
            lock = read_lock(filename, path.read_text())
            key = f"node_modules/{package}" if filename == "package-lock.json" else package
            if key in lock:
                return lock[key].version
    return None


@dataclass
class LockDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    new_install_scripts: list[str] = field(default_factory=list)

    def summary(self, limit: int = 40) -> str:
        def fmt(title: str, items: list[str]) -> str:
            more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
            return f"{title} ({len(items)}): {', '.join(items[:limit])}{more}" if items else ""
        parts = [fmt("NEW packages with install scripts", self.new_install_scripts),
                 fmt("added", self.added), fmt("removed", self.removed),
                 fmt("changed", self.changed)]
        return "\n".join(p for p in parts if p) or "no lockfile changes"


def diff_locks(filename: str, before: str, after: str) -> LockDiff:
    a, b = read_lock(filename, before), read_lock(filename, after)
    d = LockDiff()
    for key in sorted(b.keys() - a.keys()):
        d.added.append(f"{b[key].name}@{b[key].version}")
        if b[key].install_script:
            d.new_install_scripts.append(f"{b[key].name}@{b[key].version}")
    for key in sorted(a.keys() - b.keys()):
        d.removed.append(f"{a[key].name}@{a[key].version}")
    for key in sorted(a.keys() & b.keys()):
        if a[key].version != b[key].version:
            d.changed.append(f"{b[key].name} {a[key].version} -> {b[key].version}")
            if b[key].install_script and not a[key].install_script:
                d.new_install_scripts.append(f"{b[key].name}@{b[key].version}")
    return d


# -- release notes -----------------------------------------------------------------

_GITHUB_REPO = re.compile(r"github\.com[/:]([^/]+)/([^/#.]+)")


def release_notes(package: str, current: str, target: str, cwd: Path, *,
                  github_token: str = "", max_chars: int = 20000) -> str:
    """Best effort: GitHub release notes between current (exclusive) and target (inclusive)
    for npm packages. Returned text is untrusted and goes to agents as data only."""
    try:
        res = subprocess.run(["npm", "view", package, "repository.url"], cwd=cwd,
                             capture_output=True, text=True, timeout=60,
                             stdin=subprocess.DEVNULL)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    m = _GITHUB_REPO.search(res.stdout)
    if not m:
        return ""
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    try:
        r = httpx.get(f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/releases",
                      params={"per_page": 100}, headers=headers, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError:
        return ""
    lo, hi = parse_version(current), parse_version(target)
    notes, size = [], 0
    for rel in sorted(r.json(), key=lambda x: parse_version(x.get("tag_name", "")) or (0, 0, 0)):
        v = parse_version(rel.get("tag_name", "").rsplit("@", 1)[-1])
        if v is None or lo is None or hi is None or not (lo < v <= hi):
            continue
        chunk = f"## {rel.get('name') or rel['tag_name']}\n{(rel.get('body') or '')[:4000]}\n"
        if size + len(chunk) > max_chars:
            notes.append("… (truncated)")
            break
        notes.append(chunk)
        size += len(chunk)
    return "\n".join(notes)
