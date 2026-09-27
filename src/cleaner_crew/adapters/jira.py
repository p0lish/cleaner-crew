"""Jira Cloud (REST v3). Auth is email + API token (basic auth)."""

from __future__ import annotations

import httpx

from ..config import TrackerConfig
from ..models import ConnectionReport, Finding, Task, extract_fingerprint, fingerprint_marker
from .base import TaskSource


def adf(text: str) -> dict:
    """Minimal Atlassian Document Format: one paragraph per blank-line-separated block."""
    paras = [p for p in text.split("\n\n") if p.strip()]
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paras]}


def adf_to_text(node: dict | None) -> str:
    if not node:
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    sep = "\n\n" if node.get("type") == "doc" else ""
    return sep.join(adf_to_text(c) for c in node.get("content", []))


class Jira(TaskSource):
    def __init__(self, cfg: TrackerConfig, email: str, token: str):
        self.cfg = cfg
        self.http = httpx.Client(base_url=cfg.base_url.rstrip("/") + "/rest/api/3",
                                 auth=(email, token), timeout=30,
                                 headers={"Accept": "application/json"})
        self._account_id: str | None = None

    def _ok(self, r: httpx.Response) -> httpx.Response:
        if r.status_code >= 400:
            raise RuntimeError(f"Jira {r.request.method} {r.request.url.path}: "
                               f"{r.status_code} {r.text[:300]}")
        return r

    def _task(self, issue: dict) -> Task:
        f = issue["fields"]
        desc = adf_to_text(f.get("description"))
        return Task(id=issue["id"], key=issue["key"], title=f["summary"], description=desc,
                    url=f"{self.cfg.base_url.rstrip('/')}/browse/{issue['key']}",
                    labels=f.get("labels", []), fingerprint=extract_fingerprint(desc))

    def _transition(self, task: Task, status: str) -> None:
        ts = self._ok(self.http.get(f"/issue/{task.key}/transitions")).json()["transitions"]
        match = next((t for t in ts if t["to"]["name"].lower() == status.lower()), None)
        if match is None:
            raise RuntimeError(f"no transition from {task.key} to '{status}'")
        self._ok(self.http.post(f"/issue/{task.key}/transitions",
                                json={"transition": {"id": match["id"]}}))

    def _labels(self, task: Task, add: list[str] = (), remove: list[str] = ()) -> None:
        ops = [{"add": l} for l in add] + [{"remove": l} for l in remove]
        self._ok(self.http.put(f"/issue/{task.key}", json={"update": {"labels": ops}}))

    def _assign(self, task: Task, account_id: str | None) -> None:
        self._ok(self.http.put(f"/issue/{task.key}/assignee", json={"accountId": account_id}))

    # -- TaskSource ------------------------------------------------------------

    def check(self) -> ConnectionReport:
        try:
            me = self._ok(self.http.get("/myself")).json()
            self._account_id = me["accountId"]
            proj = self._ok(self.http.get(f"/project/{self.cfg.project}")).json()
        except (httpx.HTTPError, RuntimeError) as e:
            return ConnectionReport(False, "jira", detail=str(e))
        return ConnectionReport(True, "jira", me.get("emailAddress", me["displayName"]),
                                f"project {proj['key']} ({proj['name']})")

    def list_projects(self) -> list[tuple[str, str]]:
        r = self._ok(self.http.get("/project/search", params={"maxResults": 100})).json()
        return [(p["key"], p["name"]) for p in r["values"]]

    def list_statuses(self) -> list[str]:
        r = self._ok(self.http.get(f"/project/{self.cfg.project}/statuses")).json()
        return sorted({s["name"] for it in r for s in it["statuses"]})

    def fetch_candidates(self, limit: int = 20) -> list[Task]:
        c = self.cfg
        jql = (f'project = "{c.project}" AND labels = "{c.candidate_label}" '
               f'AND labels not in ("{c.in_progress_label}", "{c.rejected_label}", '
               f'"{c.escalated_label}") AND statusCategory = "To Do" ORDER BY priority DESC')
        r = self._ok(self.http.post("/search/jql", json={
            "jql": jql, "maxResults": limit,
            "fields": ["summary", "description", "labels"]})).json()
        return [self._task(i) for i in r.get("issues", [])]

    def create_finding(self, finding: Finding) -> Task:
        body = (f"{finding.description}\n\nCategory: {finding.category.value}\n\n"
                f"Files: {', '.join(finding.files)}\n\n"
                f"Filed by cleaner-crew scout. {fingerprint_marker(finding.fingerprint)}")
        r = self._ok(self.http.post("/issue", json={"fields": {
            "project": {"key": self.cfg.project}, "summary": finding.title,
            "description": adf(body), "issuetype": {"name": "Task"},
            "labels": [self.cfg.candidate_label]}})).json()
        return self._task(self._ok(self.http.get(
            f"/issue/{r['key']}", params={"fields": "summary,description,labels"})).json())

    def claim(self, task: Task) -> None:
        if self._account_id is None:
            self._account_id = self._ok(self.http.get("/myself")).json()["accountId"]
        self._labels(task, add=[self.cfg.in_progress_label])
        self._assign(task, self._account_id)
        self._transition(task, self.cfg.status_in_progress)

    def comment(self, task: Task, body: str) -> None:
        self._ok(self.http.post(f"/issue/{task.key}/comment", json={"body": adf(body)}))

    def mark_in_review(self, task: Task, mr_url: str) -> None:
        self._transition(task, self.cfg.status_in_review)
        self.comment(task, f"Cleaner crew opened a merge request: {mr_url}")

    def _release(self, task: Task, label: str, reason: str) -> None:
        self._labels(task, add=[label], remove=[self.cfg.in_progress_label])
        self._assign(task, None)
        self._transition(task, self.cfg.status_todo)
        self.comment(task, reason)

    def reject(self, task: Task, reason: str) -> None:
        self._release(task, self.cfg.rejected_label, f"Cleaner crew: not shipping this.\n\n{reason}")

    def escalate(self, task: Task, reason: str) -> None:
        self._release(task, self.cfg.escalated_label,
                      f"Cleaner crew: this needs a human.\n\n{reason}")
