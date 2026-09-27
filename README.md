# Cleaner Crew 🧹

A crew of Claude Code agents that picks up small, safe tasks from your tracker
(Linear, Jira), fixes them in isolation, and opens merge requests on GitHub or GitLab.
It only ships a change when a deterministic policy gate agrees, it never merges, and it
earns more autonomy per category only as humans accept its work.

```
scout ──► proposed tickets (cleaner-crew:proposed)
             │  a human adds the `cleaner-crew` label to the ones worth doing
candidate tickets (cleaner-crew)
             │
manager ──► claim ──► triage + plan
             │
             ├─► inspector   write a failing test that reproduces the bug (bugfix)
             ├─► janitor     implement the plan (cannot edit tests)
             ├─► inspector   cover the change with tests (can only edit tests)
             ├─► tests + lint + diff + mutation testing, run by the orchestrator
             └─► hooded      read-only security review, can veto
             │
policy gate + manager verdict + trust level ──► MR │ draft MR │ shadow │ escalate │ reject
             │
target repo CI: cleaner-crew-verify (required check) + human approval ──► merge
```

## Install into a repository

```sh
cd path/to/your/cloned/repo
uvx cleaner-crew init        # or: uv tool install cleaner-crew && cleaner-crew init
```

`init` walks you through:

1. **Code host.** It detects GitHub/GitLab from `origin`, checks the token (or an existing
   `gh` login) and verifies that it has push and MR permissions.
2. **Task manager.** You choose Linear or Jira, paste a token if one is missing, pick the
   team/project, and map the todo / in progress / in review statuses.
3. **Tests.** It detects the stack, confirms the test and lint commands, and runs the suite
   once. If the baseline is red, the crew is installed disabled.
4. **Files.** It writes `.cleaner-crew/config.yml`, `policy.yml` and `.claude/agents/cleaner-crew-*.md`.
5. **Runner.** Choose a scheduled CI job (GitHub Actions / GitLab schedule), a local
   daemon (loop or systemd user timer), or both.
6. **Protection.** It adds the `cleaner-crew-verify` CI check, checks branch protection on
   the default branch and tells you exactly what to turn on.

Secrets go to `.cleaner-crew/secrets.env` (gitignored, mode 600) or your CI secrets.
`config.yml` holds only the variable names.

## Commands

| command | what it does |
|---|---|
| `cleaner-crew init` | install and connect |
| `cleaner-crew doctor` | re-check connections, CLI, test baseline, kill switch |
| `cleaner-crew run --dry-run` | scout and plan only; nothing is claimed, filed or pushed |
| `cleaner-crew run` | one run: up to `max_tasks_per_run` tickets |
| `cleaner-crew trust` | each category's earned trust level and acceptance rate |
| `cleaner-crew verify --base main` | CI check re-applying the base branch's policy to a crew branch |
| `cleaner-crew daemon` | run in a loop on this machine |
| `cleaner-crew stop` / `resume` | kill switch (commit `.cleaner-crew/STOP` to stop CI too) |

## How it earns trust

**Humans choose the work.** The scout only *proposes* (`cleaner-crew:proposed`). Adding
the `cleaner-crew` label takes about ten seconds and is far cheaper than reviewing a
pointless MR. At most `max_open_proposals` proposals are open at once.

**Autonomy per category is measured, not assumed.** An MR counts as *accepted* if it was
merged with no human commits pushed to its branch (crew commits carry `Cleaner-Crew-*`
git trailers). Over the last `trust.window` closed MRs per category:

| record | level | behaviour |
|---|---|---|
| fewer than `min_samples` | draft | MRs are opened as drafts |
| acceptance ≥ `promote_at` (80%) | ready | MRs are opened ready for review |
| acceptance < `demote_below` (50%) | shadow | the plan is posted on the ticket; nothing changes |
| otherwise | draft | |

The earned level is capped by each category's `max_level` in `policy.yml`. After fixing
whatever caused a demotion, set `trust_since` to today's date to start a fresh record.
History is read from the code host, so CI and local runners agree.

**Tests must actually test the change.** After tests pass, the crew mutates the changed
lines (`==`→`!=`, `<`→`<=`, `n`→`n+1`, `and`→`or`, ...) and re-runs the tests. If
fewer than `mutation.min_score` of the mutants are caught, the MR becomes a draft, and
the surviving mutants are listed in the MR.

## Protecting the target repo

The crew never merges. The repository's own protections are the real safety net:

- **Branch protection** on the default branch: require at least one approving review,
  dismiss stale approvals, and require `cleaner-crew-verify` plus your test workflow.
- **`cleaner-crew-verify`** re-checks every `cleaner-crew/*` MR against the policy *from
  the base branch*. It checks forbidden paths (including the crew's own config), size
  limits, required tests, category enabled, and crew trailers present. On GitHub it runs
  as `pull_request_target`, so an MR can't edit the check that judges it, and it never
  executes the MR's code.
- **CODEOWNERS** for `/.cleaner-crew/`, `/.claude/` and CI config.

`cleaner-crew doctor` reports whether the default branch is adequately protected.

## Safety model

- **The policy gate is code, not a prompt.** `policy.yml` sets size limits, forbidden
  paths, required tests and security approval. The manager's verdict can only be made
  stricter by the policy.
- **Separate contexts.** Each role is a separate `claude -p` process. The inspector and
  hooded agent see the plan and diff, never the janitor's reasoning.
- **Separation of duties.** Janitors cannot edit tests, and inspectors can only edit tests.
  Scout, manager and hooded agents are read-only.
- **Three layers of tool restriction:** `--allowedTools` per role, the agent's `tools:`
  frontmatter, and a PreToolUse guard hook (`cleaner_crew.hooks.guard`). The guard blocks
  the network, git history commands, secrets files, anything outside the task worktree,
  and the crew's own config.
- **Agents hold no credentials.** Tracker and code host tokens are stripped from agent
  environments. Only the orchestrator claims tickets, commits, pushes and opens MRs.
- **Ticket text is untrusted.** It is wrapped as data. The hooded agent checks the diff
  against the plan and flags suspected prompt injection, which blocks the change.
- **Limits:** max open crew MRs, a per-task cost cap, a green baseline requirement, and a
  kill switch.
- **Audit trail:** every agent transcript, the test log and the outcome are kept under
  `.cleaner-crew/runs/` (uploaded as a CI artifact).

## Extending

Add a tracker or code host by implementing `TaskSource` or `CodeHost` in
`src/cleaner_crew/adapters/base.py` and registering it in `adapters/__init__.py`.
Customise agent behaviour by editing `.claude/agents/cleaner-crew-*.md` in the target repo.

## Development

```sh
uv sync
uv run pytest
```
