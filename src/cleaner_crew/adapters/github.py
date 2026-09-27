from __future__ import annotations

import os
import subprocess

import httpx

from ..models import ConnectionReport, MrRecord, category_from_labels, is_crew_commit
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
    def __init__(self, repo: str, token: str, api_url: str = "https://api.github.com"):
        self.repo = repo
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
        missing = [] if perms.get("push") else [
            "push access (contents: write, pull_requests: write)"]
        return ConnectionReport(not missing, "github", login, f"repo {self.repo}", missing)

    def count_open_mrs(self, branch_prefix: str) -> int:
        r = self.http.get(f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100})
        r.raise_for_status()
        return sum(1 for pr in r.json() if pr["head"]["ref"].startswith(branch_prefix))

    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool,
                labels: list[str]) -> str:
        r = self.http.post(f"/repos/{self.repo}/pulls", json={
            "title": title, "head": branch, "base": base, "body": body, "draft": draft})
        r.raise_for_status()
        pr = r.json()
        self.http.post(f"/repos/{self.repo}/issues/{pr['number']}/labels",
                       json={"labels": labels}).raise_for_status()
        return pr["html_url"]

    def crew_mr_history(self, branch_prefix: str, limit: int = 100) -> list[MrRecord]:
        r = self.http.get(f"/repos/{self.repo}/pulls", params={
            "state": "closed", "sort": "updated", "direction": "desc", "per_page": 100})
        r.raise_for_status()
        out = []
        for pr in r.json():
            if not pr["head"]["ref"].startswith(branch_prefix) or len(out) >= limit:
                continue
            cat = category_from_labels([label["name"] for label in pr.get("labels", [])])
            if cat is None:
                continue
            commits = self.http.get(f"/repos/{self.repo}/pulls/{pr['number']}/commits",
                                    params={"per_page": 100})
            commits.raise_for_status()
            human = any(not is_crew_commit(c["commit"]["message"]) for c in commits.json())
            out.append(MrRecord(cat, merged=pr.get("merged_at") is not None,
                                changed_by_human=human, closed_at=pr["closed_at"]))
        return out

    def branch_protection(self, branch: str) -> tuple[bool | None, str]:
        """Combines classic branch protection and rulesets (whichever the repo uses)."""
        reviews, checks, sources = 0, set(), []

        r = self.http.get(f"/repos/{self.repo}/branches/{branch}/protection")
        if r.status_code == 403 and "Upgrade to GitHub Pro" in r.text:
            return False, ("branch protection is unavailable for this private repo on "
                           "GitHub Free (upgrade to Pro or make it public)")
        if r.status_code == 200:
            p = r.json()
            reviews = (p.get("required_pull_request_reviews") or {}).get(
                "required_approving_review_count", 0)
            checks |= set((p.get("required_status_checks") or {}).get("contexts", []))
            sources.append("branch protection")
        elif r.status_code not in (404, 403):
            return None, f"cannot read protection for {branch} ({r.status_code})"

        r = self.http.get(f"/repos/{self.repo}/rules/branches/{branch}")
        if r.status_code == 200 and r.json():
            for rule in r.json():
                params = rule.get("parameters") or {}
                if rule.get("type") == "pull_request":
                    reviews = max(reviews, params.get("required_approving_review_count", 0))
                elif rule.get("type") == "required_status_checks":
                    checks |= {c.get("context", "")
                               for c in params.get("required_status_checks", [])}
            sources.append("rulesets")

        if not sources:
            return False, f"{branch} is not protected"
        crew_check = any("cleaner-crew" in c for c in checks)
        detail = (f"{branch} via {' + '.join(sources)}: {reviews} required review(s); "
                  f"required checks: {', '.join(sorted(checks)) or 'none'}")
        if not crew_check:
            detail += " (cleaner-crew-verify is not required)"
        return bool(reviews) and crew_check, detail
