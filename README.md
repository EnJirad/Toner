# Toner

Toner is a reusable AI coding agent and a small software factory. It works on
the project configured in `settings.json` and is not tied to a specific
repository or language.

A task is **not** marked done because a model says it is done. Toner runs the
change through separate roles and only accepts a task when independently
executed evidence satisfies its acceptance checks.

```
Planner → Task ledger → Builder → Reviewer → QA → Verifier → Evidence
                                                              ↓
                                                   Checkpoint → Commit → DONE
```

## Requirements

- Python 3.10 or newer
- `requests` (only needed for model calls; `/status`, `/resume` and `/report`
  work without it)
- An API endpoint compatible with the Chat Completions API
- A model/provider that supports tool calling
- Git is recommended (not required) for diffs, checkpoints and commits

## Installation on Termux

```bash
pkg update
pkg install python git
python -m pip install requests
```

Create or clone the Toner files, then allow shared-storage access if needed:

```bash
termux-setup-storage
```

## API key

Set the key in the environment:

```bash
export AIUNLIMIT_API_KEY='YOUR_API_KEY'
```

Alternatively set `api_key` in `settings.json`. Never commit it.

## Configuration

`settings.json` is centralized and backwards compatible with earlier versions.

| Key | Default | Purpose |
| --- | --- | --- |
| `project_root` | required | Project Toner may modify |
| `api_url`, `model`, `api_key` | required for model calls | Provider settings |
| `provider` | `{"type": "openai_compatible"}` | Provider adapter; only OpenAI-compatible is implemented |
| `models` | `{}` | Optional per-role model override, e.g. `{"reviewer": "..."}` |
| `state_directory` | `.toner` | Relative state directory inside the project |
| `max_tool_rounds` | 50 | Tool-call rounds per build |
| `request_timeout` | 120 | Model request timeout (seconds) |
| `command_timeout` | 180 | Shell command timeout (seconds) |
| `max_file_bytes` | 100000 | File read/write limit |
| `max_search_results` | 50 | Search result limit |
| `max_auto_steps_per_session` | 10 | Tasks attempted per run |
| `max_plan_files` / `max_plan_tasks` | 100 / 30 | Plan size limits |
| `max_context_chars` | 24000 | Conversation budget before compaction |
| `max_attempts` | 3 | Attempts per task before it is blocked |
| `max_identical_failures` | 2 | Identical failure signature limit |
| `retry_backoff_seconds` / `retry_backoff_max_seconds` | 2 / 30 | Retry delay |
| `auto_commit` | `false` | Commit only after verification |
| `require_acceptance_checks` | `true` | Require machine-checkable criteria |
| `batch_limits` | low 20, medium 5, high 1 | Risk-aware batch caps per run |
| `test_command` | empty | Optional project-wide check |
| `require_approval_for_*` | see `settings.json` | Interactive approvals |
| `ignored_directories` | build/VCS dirs | Never walked or searched |
| `project_rule_files` | `.ai/*`, `AGENTS.md` | Extra project rules |

Environment overrides: `AIUNLIMIT_API_KEY`, `TONER_PROJECT_ROOT`,
`TONER_API_URL`, `TONER_MODEL`, `TONER_STATE_DIR`, `TONER_SETTINGS`.

## Select a project

```json
{
  "project_root": "/storage/emulated/0/VNE/Vel/velnox"
}
```

The project must already exist. Toner creates its own `.toner/` workspace there.

## Start Toner

```bash
python toner.py
```

## Commands

- `/check` — read `.toner/new_command.md` and run it as one ad-hoc task
- `/auto` — read `.toner/files_md/*.md`, plan tasks, then build/review/test/verify
- `/status` — show the run, tasks, attempts, last error, commits and test totals
- `/resume` — validate saved state, show recovery details and continue safely
- `/report` — write an evidence-based Markdown engineering report
- `/help` — show help
- `/exit` — exit

Plain text is treated as an ad-hoc command, as before.

## How `/auto` works

1. Markdown plans in `.toner/files_md/` are fingerprinted and sent to the
   Planner, which must return validated JSON tasks with acceptance criteria and
   **machine-checkable** `checks` (`command`, `file_exists`, `file_absent`,
   `text_contains`).
2. Each task is persisted in `.toner/tasks/TASK-000001.json`.
3. A task runs through explicit states: `QUEUED → BUILDING → REVIEWING →
   TESTING → VERIFYING → COMMITTING → DONE`.
4. The Builder may only use the Tool Registry, cannot commit, and cannot mark a
   task done.
5. The Reviewer sees the real diff and the commands that actually ran.
6. QA executes allowlisted check commands and records exit codes, stdout,
   stderr, duration and timeouts. A skipped check is never a pass.
7. The Verifier is deterministic: it requires passing executed checks, a clean
   review, and every machine-checkable criterion. Otherwise the task is retried
   or blocked. Only the Verifier can authorize `DONE`.
8. Evidence is written to `.toner/evidence/<task>/` (`build.json`, `diff.json`,
   `review.json`, `tests.json`, `verification.json`, plus attempt snapshots).
9. Checkpoints record the Git SHA and task state before and after risky work.

### Retries and blocking

Failures are fingerprinted (type, file, line, normalized message) and classified
as transient, recoverable, permanent or dangerous.

- transient/recoverable failures retry within `max_attempts`
- the same signature repeating `max_identical_failures` times blocks the task
- permanent and dangerous failures block immediately
- a blocked task's dependants are blocked; independent tasks continue
- blocked tasks keep their evidence and fingerprints for review

### Risk-aware batching

`batch_limits` caps how many tasks of each risk level are attempted per run.
Authentication, payments, migrations, security and production work are treated
as high risk and stop after one task so it can be reviewed.

### Commits

`auto_commit` is **off by default**. When enabled, Toner commits only after
verification, only the files the task changed, and refuses if unrelated files
are already staged. It never force-pushes, hard-resets or cleans.

## Workspace created in the target project

```text
.toner/
├── files_md/                  # plan input (you write these)
├── new_command.md             # /check input
├── tasks/TASK-000001.json     # task ledger
├── runs/RUN-000001.json       # run records
├── evidence/TASK-000001/      # tests, review, verification, diff
├── checkpoints/               # state + Git SHA snapshots
├── failures/TASK-000001.json  # failure fingerprints and decisions
├── state/                     # session, active run, events.jsonl
├── audit/                     # audit reports (this repository)
└── ai_recheck/
    ├── backups/               # file backups (not readable by the agent)
    ├── reports/               # /report output
    └── auto_state.json        # legacy state, imported once
```

## Recovery

State is saved before important transitions, so after Ctrl+C, a crash or a
network failure Toner can answer: what was running, what completed, what
failed, which commit was known good, and what should happen next.

`/resume`:

1. validates the task ledger (unknown statuses and missing dependencies are
   reported instead of guessed),
2. refuses to continue silently when plan Markdown changed,
3. shows task ID, status, phase, attempts, last error, failure fingerprint,
   last known good commit, current commit and the last test totals,
4. records an unclean previous run as `abandoned`,
5. requeues paused/failed tasks and continues.

Legacy `.toner/ai_recheck/auto_state.json` plans are imported once. Those tasks
had no machine checks, so they are verified from executed QA plus review and are
marked accordingly.

## Safety

- Paths must stay inside `project_root`; traversal and symlink escapes are
  rejected.
- `.env*`, credential, key and certificate files are never readable or
  writable by the agent; `.git` internals and Toner's own state are blocked.
- File writes are atomic, existing files are backed up first, and edits require
  exactly one match.
- Shell commands are screened for destructive patterns, receive a filtered
  environment (no secrets), run inside the project root, and are killed on
  timeout.
- Secret values, bearer tokens, API keys and URL credentials are redacted from
  tool output, logs and evidence.
- Deletions require explicit confirmation and `allow_file_deletion`.
- Destructive Git operations are not implemented at all.

## Tests

The suite uses the Python standard library only:

```bash
python3 -m unittest discover -s tests -t .
```

Coverage includes unit tests, golden safety/tool-contract tests and integration
tests for the full pipeline, blocking, commits, resume, planning and the CLI.
No test requires network access; model calls are scripted.

## Troubleshooting

### API authentication error
Check `AIUNLIMIT_API_KEY`, the provider URL and the model name.

### Unknown model or tool-calling error
Verify the provider supports the selected model and tool calling.

### Project path error
Check that `project_root` exists and Termux can access it.

### Resume refuses to continue
The Markdown plan changed. Re-plan with `/auto`, or review the saved tasks in
`.toner/tasks/`.

### A task is blocked
Read `.toner/evidence/<task>/verification.json` and
`.toner/failures/<task>.json`. Fix the cause, then use `/resume`. A blocked task
is never retried forever.

## Known limitations

- Only the OpenAI-compatible provider is implemented. Other providers require a
  `ModelProvider` adapter; no untested adapters are shipped.
- QA only runs allowlisted check commands automatically. Anything else is
  recorded as skipped and prevents automated verification.
- `/check` is an ad-hoc build and is **not** evidence-gated; use `/auto` for
  verified work.
- Git is optional, but without it Toner cannot produce a reviewable diff or
  commits.
