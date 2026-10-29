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

## Two ways to use Toner

**Conversation.** Type normally. Every message is classified before anything
runs, so `hi` or `สวัสดี` gets a reply instead of starting a build, and
`ทำไม build ไม่ผ่าน` triggers read-only diagnosis with the user's language
preserved.

**Coding tasks.** A request to create, fix, refactor or implement something
starts a tracked task: baseline snapshot, inspected files, minimal edits, real
checks, and a report of exactly what changed and what was verified.

Neither needs a slash command. Slash commands are only for explicit control.

## Requirements

- Python 3.10 or newer
- `requests` (only needed for model calls; `/status`, `/resume` and `/report`
  work without it)
- An API endpoint compatible with the Chat Completions API
- A model/provider that supports tool calling. Providers that ignore the
  `tools` parameter still work through the validated fenced-block protocol
  described below
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

Slash commands are control commands only. Any other line is natural language
and goes to the intent router — it is never turned into a command just because
it lacks a leading `/`.

- `/help` — commands, workflow and routing examples
- `/status` — run, task states, attempts, model, project root, last error, totals
- `/check [text]` — read `.toner/new_command.md` (or use the given text) and run
  it as one ad-hoc task. Ad-hoc by design: not evidence-gated, so use `/auto`
  for verified work
- `/auto` — read `.toner/files_md/*.md`, plan tasks, then build/review/test/verify
- `/resume` — validate saved state, show recovery details and continue safely
- `/report` — write an evidence-based Markdown engineering report
- `/stop` — request cancellation of the active task; the current step finishes,
  state is saved, and no further step starts
- `/exit`, `/quit` — exit cleanly

## How conversation routing works

`core/intent.py` classifies every non-command message into one category. It is
pure logic (no terminal, no filesystem, no tools) and therefore unit testable
in isolation. Routing is a hybrid:

1. a deterministic fast path for greetings, pleasantries and follow-ups,
2. model-based classification when a model is reachable — this is what handles
   requests no keyword list would anticipate,
3. a keyword heuristic when no model is configured or the call fails.

| Intent | What Toner does |
| --- | --- |
| `CHAT` | Replies conversationally with history. No tools, no Git, no files, no evidence |
| `QUESTION` | Read-only inspection (read/search/list only) then explains the finding |
| `INSPECT` | Same read-only loop; explicitly diagnoses without editing |
| `PLAN` | Produces a plan and states that no files were changed |
| `CODE_TASK` | Starts a tracked task through build/review/QA/verification |
| `TEST` | Runs the allowlisted command from the message and/or `test_command` |
| `GIT_TASK` | Read-only Git inspection (status, log, diff, branch) |
| `CLARIFICATION` | Asks one concise question instead of guessing |

Follow-ups such as `ทำต่อ`, `แก้ต่อเลย` and `ทดสอบอีกครั้ง` reuse the active
task context; when there is no unfinished task they say so instead of inventing
work. `CHAT`/`INSPECT`/`PLAN` can never write files or run commands: the agent
loop is restricted to an explicit read-only tool allowlist, and a write attempt
is refused and recorded.

## How the agent loop works

`core/agent_loop.py` is the single controlled loop used by the Builder and by
inspection. Each round sends the tool schemas, receives one or more calls,
executes them through the Tool Registry, and feeds the real results back.

Tool calls are accepted in exactly two validated formats:

1. **Native** — Chat-Completions `tool_calls` on the assistant message.
2. **Text protocol** — providers that ignore `tools` emit a fenced block:

   ```tool_call
   {"name": "read_file", "arguments": {"path": "app.py"}}
   ```

Both are validated against the registry schema (unknown tool, wrong type,
missing argument and path violations are rejected and reported). Prose that
merely *describes* a tool never becomes a call, so Toner cannot claim a tool ran
when only text was produced. The loop enforces `max_tool_rounds`, request and
command timeouts, a repeated-identical-call guard, and cooperative cancellation
(`/stop` or Ctrl+C).

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
   `review.json`, `tests.json`, `verification.json`, `changes.json`, plus
   attempt snapshots).
9. Checkpoints record the Git SHA and task state before and after risky work.

### Change attribution

Before a task starts, Toner captures a baseline: `git status --porcelain` plus
the set of already-dirty and untracked paths. After the task it compares that
baseline with the final status and the file writes the tools actually performed,
then reports four separate buckets:

- **Changed by this task** — new or modified paths Toner's own tools touched
- **Pre-existing changes** — dirty before the task, not touched by it
- **Untracked files that existed before this task** — never claimed as output
- **Unattributed** — differences no tool can be proven to have caused (for
  example a shell command or another process)

Files dirty before the task are listed separately again when Toner also changed
them. Pre-existing content is never discarded, and no path is ever reported as
"changed by this task" on the strength of `git status` alone. If Git is
unavailable, attribution falls back to observed tool writes only and the report
states that limitation explicitly.

Checks are reported with mutually exclusive counts: executed, passed, failed,
skipped, and not run. A skipped or missing check is never counted as a pass and
never satisfies verification.

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

The current suite has 190 tests. Routing and conversation coverage lives in
`tests/unit/test_intent.py`, `tests/unit/test_toolcall.py`,
`tests/unit/test_agent_loop.py`, `tests/unit/test_baseline.py` and
`tests/integration/test_conversation.py`. The integration suite drives
`Orchestrator.handle()` exactly as the CLI does and asserts that greetings run
no tools or evidence, inspection cannot write files, pre-existing user changes
are not attributed to the task, unexecuted checks are never reported as
passing, cancellation stops further work, and API/tool-format errors are
reported instead of crashing.

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
  commits, and change attribution falls back to observed tool writes only.
- Conversation (`CHAT`) is intentionally toolless, and `QUESTION`/`INSPECT`
  are restricted to read-only tools. Asking a question will not silently edit
  the project — say "fix it" to start a task.
- `TEST` only executes commands the security policy allowlists. Anything else
  is refused with an explanation; Toner will not run arbitrary shell lines just
  because they were typed.
- The text tool protocol is fully validated, but providers vary in how
  reliably they emit it. Native tool calling is always preferred.
- Everything in this repository is verified with scripted model responses on
  Linux/Python 3.10. No test in this environment used a live provider key.
