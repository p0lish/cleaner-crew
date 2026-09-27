from __future__ import annotations

import httpx

from ..config import TrackerConfig
from ..models import ConnectionReport, Finding, Task, extract_fingerprint, fingerprint_marker
from .base import TaskSource

API = "https://api.linear.app/graphql"

ISSUE_FIELDS = "id identifier title description url labels { nodes { name } }"


class Linear(TaskSource):
    def __init__(self, cfg: TrackerConfig, api_key: str):
        self.cfg = cfg
        self.http = httpx.Client(headers={"Authorization": api_key}, timeout=30)
        self._team: dict | None = None
        self._label_ids: dict[str, str] = {}
        self._viewer_id: str | None = None

    def _q(self, query: str, **variables) -> dict:
        r = self.http.post(API, json={"query": query, "variables": variables})
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(f"Linear API error: {body['errors'][0].get('message')}")
        return body["data"]

    # -- lookups ---------------------------------------------------------------

    @property
    def team(self) -> dict:
        if self._team is None:
            nodes = self._q(
                "query($k:String!){teams(filter:{key:{eq:$k}}){nodes{id key name "
                "states{nodes{id name type}}}}}", k=self.cfg.project)["teams"]["nodes"]
            if not nodes:
                raise RuntimeError(f"Linear team '{self.cfg.project}' not found")
            self._team = nodes[0]
        return self._team

    def _state_id(self, name: str) -> str:
        for s in self.team["states"]["nodes"]:
            if s["name"].lower() == name.lower():
                return s["id"]
        raise RuntimeError(f"Linear state '{name}' not found in team {self.cfg.project}")

    def _label_id(self, name: str) -> str:
        if name not in self._label_ids:
            nodes = self._q("query($n:String!){issueLabels(filter:{name:{eq:$n}}){nodes{id}}}",
                            n=name)["issueLabels"]["nodes"]
            if nodes:
                self._label_ids[name] = nodes[0]["id"]
            else:
                self._label_ids[name] = self._q(
                    "mutation($n:String!,$t:String!){issueLabelCreate(input:{name:$n,teamId:$t})"
                    "{issueLabel{id}}}", n=name, t=self.team["id"]
                )["issueLabelCreate"]["issueLabel"]["id"]
        return self._label_ids[name]

    def _viewer(self) -> str:
        if self._viewer_id is None:
            self._viewer_id = self._q("{viewer{id}}")["viewer"]["id"]
        return self._viewer_id

    def _task(self, n: dict) -> Task:
        return Task(id=n["id"], key=n["identifier"], title=n["title"],
                    description=n.get("description") or "", url=n["url"],
                    labels=[l["name"] for l in n["labels"]["nodes"]],
                    fingerprint=extract_fingerprint(n.get("description") or ""))

    def _update(self, task: Task, **input_) -> None:
        self._q("mutation($id:String!,$in:IssueUpdateInput!){issueUpdate(id:$id,input:$in)"
                "{success}}", id=task.id, **{"in": input_})

    # -- TaskSource ------------------------------------------------------------

    def check(self) -> ConnectionReport:
        try:
            v = self._q("{viewer{id name email}}")["viewer"]
        except (httpx.HTTPError, RuntimeError) as e:
            return ConnectionReport(False, "linear", detail=str(e))
        try:
            team = self.team
        except RuntimeError as e:
            return ConnectionReport(False, "linear", v["email"], str(e))
        return ConnectionReport(True, "linear", v["email"], f"team {team['key']} ({team['name']})")

    def list_projects(self) -> list[tuple[str, str]]:
        return [(t["key"], t["name"])
                for t in self._q("{teams{nodes{key name}}}")["teams"]["nodes"]]

    def list_statuses(self) -> list[str]:
        return [s["name"] for s in self.team["states"]["nodes"]]

    def fetch_candidates(self, limit: int = 20) -> list[Task]:
        nodes = self._q(
            "query($k:String!,$l:String!,$n:Int!){issues(first:$n,filter:{"
            "team:{key:{eq:$k}},labels:{name:{eq:$l}},"
            "state:{type:{nin:[\"started\",\"completed\",\"canceled\"]}}})"
            f"{{nodes{{{ISSUE_FIELDS}}}}}}}",
            k=self.cfg.project, l=self.cfg.candidate_label, n=limit)["issues"]["nodes"]
        blocked = {self.cfg.in_progress_label, self.cfg.rejected_label, self.cfg.escalated_label}
        return [t for t in map(self._task, nodes) if not blocked & set(t.labels)]

    def create_finding(self, finding: Finding) -> Task:
        body = (f"{finding.description}\n\n**Category:** {finding.category.value}  \n"
                f"**Files:** {', '.join(f'`{f}`' for f in finding.files)}\n\n"
                f"_Filed by cleaner-crew scout._ {fingerprint_marker(finding.fingerprint)}")
        n = self._q(
            "mutation($in:IssueCreateInput!){issueCreate(input:$in)"
            f"{{issue{{{ISSUE_FIELDS}}}}}}}",
            **{"in": {"teamId": self.team["id"], "title": finding.title, "description": body,
                      "labelIds": [self._label_id(self.cfg.candidate_label)],
                      "stateId": self._state_id(self.cfg.status_todo)}},
        )["issueCreate"]["issue"]
        return self._task(n)

    def claim(self, task: Task) -> None:
        self._update(task, stateId=self._state_id(self.cfg.status_in_progress),
                     assigneeId=self._viewer(),
                     addedLabelIds=[self._label_id(self.cfg.in_progress_label)])

    def comment(self, task: Task, body: str) -> None:
        self._q("mutation($in:CommentCreateInput!){commentCreate(input:$in){success}}",
                **{"in": {"issueId": task.id, "body": body}})

    def mark_in_review(self, task: Task, mr_url: str) -> None:
        self._update(task, stateId=self._state_id(self.cfg.status_in_review))
        self.comment(task, f"Cleaner crew opened a merge request: {mr_url}")

    def _release(self, task: Task, label: str, reason: str) -> None:
        self._update(task, stateId=self._state_id(self.cfg.status_todo), assigneeId=None,
                     addedLabelIds=[self._label_id(label)],
                     removedLabelIds=[self._label_id(self.cfg.in_progress_label)])
        self.comment(task, reason)

    def reject(self, task: Task, reason: str) -> None:
        self._release(task, self.cfg.rejected_label, f"Cleaner crew: not shipping this.\n\n{reason}")

    def escalate(self, task: Task, reason: str) -> None:
        self._release(task, self.cfg.escalated_label,
                      f"Cleaner crew: this needs a human.\n\n{reason}")
