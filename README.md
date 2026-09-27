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
             (dependency upgrades: supply run → quartermaster → inspector, see below)
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

## Dependency upgrades

Off by default. Enable `categories.dependency-upgrade` in `policy.yml`. The rule of this
flow is that **network access and AI never mix**:

1. **Scout** (no AI): runs `commands.outdated` (e.g. `npm outdated --json`) and proposes
   upgrades whose semver jump is in `allowed` (patch/minor by default; 0.x minors count
   as major). Patch upgrades come first. A human promotes them as usual.
2. **Manager** plans the migration and names the exact package and version.
3. **Policy** (code) checks the jump against the locked version. Majors go to a human.
4. **Supply run** (orchestrator, network, no AI): `commands.install` with install scripts
   disabled, then `commands.post_install` (e.g. `npx playwright install chromium`). It
   verifies the lockfile now pins the target version and fetches release notes.
5. **Quartermaster** (AI, no network): migrates the code to the new version using the
   release notes. It cannot install anything, edit tests or write lockfiles.
6. **Inspector** adapts tests only if the upgrade broke them, and never loosens assertions.
7. **Hooded** gets a structured lockfile diff: new transitive packages and new install
   scripts are called out for review.

Lockfiles don't count towards size limits and are never shown raw to agents.

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

`cleaner-crew doctor` reports whether the default branch is adequately protected. Both
classic branch protection and rulesets are recognised.

**No branch protection available** (private repos on GitHub Free): nothing can enforce
reviews or required checks. The crew then:
- only ever pushes `cleaner-crew/*` branches (checked in code before every push)
- caps every category at **draft**, so a human must click "Ready for review"
  (`trust.require_protection_for_ready`)
- installs `cleaner-crew-watchdog`, a workflow that fails loudly if a crew commit reaches
  the default branch without a pull request

The real fix is GitHub Pro or a public repo.

### CI secrets (GitHub)

| secret | what |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` *or* `ANTHROPIC_API_KEY` | `claude setup-token` for a Pro/Max subscription, or an API key |
| `CLEANER_CREW_TOKEN` | fine-grained token for this repo: Contents + Pull requests read/write. PRs opened with the built-in `GITHUB_TOKEN` don't trigger other workflows, so `cleaner-crew-verify` would never run |
| `LINEAR_API_KEY` *or* `JIRA_EMAIL` + `JIRA_API_TOKEN` | tracker access |

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
uv run ruff check src tests
```

CI (`.github/workflows/ci.yml`) runs lint, the tests on Python 3.11–3.14, and a packaging
check (every template is in the wheel, and the installed wheel runs) on every push and PR.

## Releasing

1. Bump `__version__` in `src/cleaner_crew/__init__.py` in a pull request and merge it.
   This is the only place the version lives.
2. `git checkout main && git pull && scripts/release.sh`

The script tags `main` as `vX.Y.Z` and pushes the tag. The `release` workflow then runs the
full CI, checks that the tag matches `__version__`, builds, publishes to PyPI through
trusted publishing (`pypi` environment), and creates the GitHub release with the
distributions attached.

## License

[Apache License 2.0](LICENSE)
