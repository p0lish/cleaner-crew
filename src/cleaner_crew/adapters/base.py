"""Adapter interfaces. Add a new tracker or code host by implementing one of these."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ConnectionReport, Finding, Task


class TaskSource(ABC):
    """Jira, Linear, ... The tracker is the source of truth for what the crew works on."""

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
        """Open tickets labelled as candidates and not yet claimed, rejected or escalated."""

    @abstractmethod
    def create_finding(self, finding: Finding) -> Task:
        """File a scout finding as a new candidate ticket."""

    @abstractmethod
    def claim(self, task: Task) -> None:
        """Mark in progress so no other run (CI or daemon) picks it up."""

    @abstractmethod
    def comment(self, task: Task, body: str) -> None: ...

    @abstractmethod
    def mark_in_review(self, task: Task, mr_url: str) -> None: ...

    @abstractmethod
    def reject(self, task: Task, reason: str) -> None:
        """Release the claim and label as rejected so it is not picked again."""

    @abstractmethod
    def escalate(self, task: Task, reason: str) -> None:
        """Release the claim and hand over to a human."""

    def find_by_fingerprint(self, fp: str) -> Task | None:
        return next((t for t in self.fetch_candidates(limit=100) if t.fingerprint == fp), None)


class CodeHost(ABC):
    """GitHub, GitLab, ..."""

    @abstractmethod
    def check(self) -> ConnectionReport:
        """Verify credentials and push/MR permissions on the repository."""

    @abstractmethod
    def count_open_mrs(self, branch_prefix: str) -> int: ...

    @abstractmethod
    def open_mr(self, branch: str, base: str, title: str, body: str, draft: bool) -> str:
        """Open a merge/pull request and return its URL."""
