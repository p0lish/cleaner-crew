from __future__ import annotations

from urllib.parse import quote

import httpx

from ..models import ConnectionReport, MrRecord, category_from_labels, is_crew_commit
from .base import CodeHost

DEVELOPER_ACCESS = 30


class GitLab(CodeHost):
    def __init__(self, repo: str, token: str, api_url: str = "https://gitlab.com/api/v4"):
        self.project = quote(repo, safe="")
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

    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool,
                labels: list[str]) -> str:
        r = self.http.post(f"/projects/{self.project}/merge_requests", json={
            "source_branch": branch, "target_branch": base,
            "title": f"Draft: {title}" if draft else title,
            "description": body, "labels": ",".join(labels), "remove_source_branch": True})
        r.raise_for_status()
        return r.json()["web_url"]

    def crew_mr_history(self, branch_prefix: str, limit: int = 100) -> list[MrRecord]:
        out = []
        for state in ("merged", "closed"):
            r = self.http.get(f"/projects/{self.project}/merge_requests", params={
                "state": state, "order_by": "updated_at", "per_page": 100})
            r.raise_for_status()
            for mr in r.json():
                if not mr["source_branch"].startswith(branch_prefix):
                    continue
                cat = category_from_labels(mr.get("labels", []))
                if cat is None:
                    continue
                commits = self.http.get(
                    f"/projects/{self.project}/merge_requests/{mr['iid']}/commits")
                commits.raise_for_status()
                human = any(not is_crew_commit(c["message"]) for c in commits.json())
                out.append(MrRecord(cat, merged=state == "merged", changed_by_human=human,
                                    closed_at=mr.get("merged_at") or mr.get("closed_at") or ""))
        out.sort(key=lambda m: m.closed_at, reverse=True)
        return out[:limit]

    def branch_protection(self, branch: str) -> tuple[bool | None, str]:
        r = self.http.get(f"/projects/{self.project}/protected_branches/{quote(branch, safe='')}")
        if r.status_code == 404:
            return False, f"{branch} is not protected"
        if r.status_code != 200:
            return None, f"cannot read protection for {branch} ({r.status_code})"
        pushers = [a.get("access_level_description")
                   for a in r.json().get("push_access_levels", [])]
        return True, f"{branch} protected; push allowed for: {', '.join(pushers) or 'no one'}"
