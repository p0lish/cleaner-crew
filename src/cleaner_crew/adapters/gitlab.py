from __future__ import annotations

from urllib.parse import quote

import httpx

from ..models import ConnectionReport
from .base import CodeHost

DEVELOPER_ACCESS = 30


class GitLab(CodeHost):
    def __init__(self, repo: str, token: str, api_url: str = "https://gitlab.com/api/v4",
                 label: str = "cleaner-crew"):
        self.project, self.label = quote(repo, safe=""), label
        self.repo = repo
        self.http = httpx.Client(base_url=api_url, headers={"PRIVATE-TOKEN": token}, timeout=30)

    def check(self) -> ConnectionReport:
        r = self.http.get("/user")
        if r.status_code != 200:
            return ConnectionReport(False, "gitlab", detail=f"auth failed ({r.status_code})")
        user = r.json()["username"]
        r = self.http.get(f"/projects/{self.project}")
        if r.status_code != 200:
            return ConnectionReport(False, "gitlab", user,
                                    f"project {self.repo} not accessible ({r.status_code})")
        perms = r.json().get("permissions") or {}
        level = max((perms.get("project_access") or {}).get("access_level", 0),
                    (perms.get("group_access") or {}).get("access_level", 0))
        missing = [] if level >= DEVELOPER_ACCESS else ["Developer role (push + create MR)"]
        return ConnectionReport(not missing, "gitlab", user, f"project {self.repo}", missing)

    def count_open_mrs(self, branch_prefix: str) -> int:
        r = self.http.get(f"/projects/{self.project}/merge_requests",
                          params={"state": "opened", "per_page": 100})
        r.raise_for_status()
        return sum(1 for mr in r.json() if mr["source_branch"].startswith(branch_prefix))

    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool) -> str:
        r = self.http.post(f"/projects/{self.project}/merge_requests", json={
            "source_branch": branch, "target_branch": base,
            "title": f"Draft: {title}" if draft else title,
            "description": body, "labels": self.label, "remove_source_branch": True})
        r.raise_for_status()
        return r.json()["web_url"]
