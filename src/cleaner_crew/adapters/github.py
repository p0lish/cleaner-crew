from __future__ import annotations

import os
import subprocess

import httpx

from ..models import ConnectionReport
from .base import CodeHost


def github_token(token_env: str) -> str:
    tok = os.environ.get(token_env, "")
    if tok:
        return tok
    # Fall back to an existing gh CLI login for local use.
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


class GitHub(CodeHost):
    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com",
                 label: str = "cleaner-crew"):
        self.repo, self.label = repo, label
        self.http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
            timeout=30,
        )

    def check(self) -> ConnectionReport:
        r = self.http.get("/user")
        if r.status_code != 200:
            return ConnectionReport(False, "github", detail=f"auth failed ({r.status_code})")
        login = r.json()["login"]
        r = self.http.get(f"/repos/{self.repo}")
        if r.status_code != 200:
            return ConnectionReport(False, "github", login,
                                    f"repo {self.repo} not accessible ({r.status_code})")
        perms = r.json().get("permissions", {})
        missing = [] if perms.get("push") else ["push access (contents: write, pull_requests: write)"]
        return ConnectionReport(not missing, "github", login, f"repo {self.repo}", missing)

    def count_open_mrs(self, branch_prefix: str) -> int:
        r = self.http.get(f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100})
        r.raise_for_status()
        return sum(1 for pr in r.json() if pr["head"]["ref"].startswith(branch_prefix))

    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool) -> str:
        r = self.http.post(f"/repos/{self.repo}/pulls", json={
            "title": title, "head": branch, "base": base, "body": body, "draft": draft})
        r.raise_for_status()
        pr = r.json()
        self.http.post(f"/repos/{self.repo}/issues/{pr['number']}/labels",
                       json={"labels": [self.label]})
        return pr["html_url"]
