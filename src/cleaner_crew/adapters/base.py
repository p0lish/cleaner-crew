"""Adapter interfaces. Add a new tracker or code host by implementing one of these."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import TrackerConfig
from ..models import ConnectionReport, Finding, MrRecord, Task


class TaskSource(ABC):
    """Jira, Linear, ... The tracker is the source of truth for what the crew works on."""

    cfg: TrackerConfig

    @abstractmethod
    def check(self) -> ConnectionReport:
        """Verify credentials and that the configured project exists."""

    @abstractmethod
    def list_projects(self) -> list[tuple[str, str]]:
        """(key, name) pairs, for the installer."""

    @abstractmethod
    def list_statuses(self) -> list[str]:
        """Workflow status names of the configured project, for the installer."""

    @abstractmethod
    def fetch_candidates(self, limit: int = 20) -> list[Task]:
        """Open tickets a human labelled as candidates, not yet claimed or settled by the crew."""

    @abstractmethod
    def find_issues(self, labels: list[str], open_only: bool, limit: int = 100) -> list[Task]:
        """Tickets carrying any of `labels`."""

    @abstractmethod
    def create_finding(self, finding: Finding) -> Task:
        """File a scout finding as a *proposed* ticket. Only a human makes it a candidate."""

    @abstractmethod
    def claim(self, task: Task) -> None:
        """Mark in progress so no other run (CI or daemon) picks it up."""

    @abstractmethod
    def comment(self, task: Task, body: str) -> None: ...

    @abstractmethod
    def mark_in_review(self, task: Task, mr_url: str) -> None: ...

    @abstractmethod
    def release(self, task: Task, label: str, comment: str) -> None:
        """Drop the claim, return the ticket to todo, add `label` and comment."""

    def reject(self, task: Task, reason: str) -> None:
        self.release(task, self.cfg.rejected_label, f"Cleaner crew: not shipping this.\n\n{reason}")

    def escalate(self, task: Task, reason: str) -> None:
        self.release(task, self.cfg.escalated_label,
                     f"Cleaner crew: this needs a human.\n\n{reason}")

    def shadow(self, task: Task, plan: str) -> None:
        self.release(task, self.cfg.shadow_label,
                     "Cleaner crew (shadow mode): this is what I would have done. "
                     f"Nothing was changed.\n\n{plan}")

    def blocked_labels(self) -> set[str]:
        c = self.cfg
        return {c.in_progress_label, c.rejected_label, c.escalated_label, c.shadow_label}

    def all_crew_labels(self) -> list[str]:
        c = self.cfg
        return [c.candidate_label, c.proposed_label, *sorted(self.blocked_labels())]

    def find_by_fingerprint(self, fp: str) -> Task | None:
        """Any ticket the crew ever filed or touched, open or closed, so nothing is re-filed."""
        issues = self.find_issues(self.all_crew_labels(), open_only=False, limit=250)
        return next((t for t in issues if t.fingerprint == fp), None)

    def count_open_proposals(self) -> int:
        issues = self.find_issues([self.cfg.proposed_label], open_only=True)
        return sum(1 for t in issues if self.cfg.candidate_label not in t.labels)


class CodeHost(ABC):
    """GitHub, GitLab, ..."""

    @abstractmethod
    def check(self) -> ConnectionReport:
        """Verify credentials and push/MR permissions on the repository."""

    @abstractmethod
    def count_open_mrs(self, branch_prefix: str) -> int: ...

    @abstractmethod
    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool,
                labels: list[str]) -> str:
        """Open a merge/pull request and return its URL."""

    @abstractmethod
    def crew_mr_history(self, branch_prefix: str, limit: int = 100) -> list[MrRecord]:
        """Recently closed crew MRs with their category, merge state and human changes."""

    @abstractmethod
    def branch_protection(self, branch: str) -> tuple[bool | None, str]:
        """(adequately protected?, detail). None when it can't be determined.

        Adequate means humans must approve before merge and, where the host exposes it,
        the cleaner-crew-verify check is required.
        """
