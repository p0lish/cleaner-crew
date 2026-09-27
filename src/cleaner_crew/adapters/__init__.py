from __future__ import annotations

from ..config import Config, env
from .base import CodeHost, TaskSource


def make_tracker(cfg: Config) -> TaskSource:
    t = cfg.tracker
    if t.kind == "linear":
        from .linear import Linear
        return Linear(t, env(t.token_env))
    if t.kind == "jira":
        from .jira import Jira
        return Jira(t, env(t.email_env), env(t.token_env))
    raise ValueError(f"unknown tracker kind: {t.kind}")


def make_code_host(cfg: Config) -> CodeHost:
    c = cfg.code_host
    if c.kind == "github":
        from .github import GitHub, github_token
        tok = github_token(c.token_env)
        if not tok:
            raise RuntimeError(f"no GitHub token: set {c.token_env} or run `gh auth login`")
        return GitHub(c.repo, tok, c.api_url)
    if c.kind == "gitlab":
        from .gitlab import GitLab
        return GitLab(c.repo, env(c.token_env), c.api_url)
    raise ValueError(f"unknown code host kind: {c.kind}")


__all__ = ["CodeHost", "TaskSource", "make_code_host", "make_tracker"]
