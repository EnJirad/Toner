# TONER V2 UPGRADE REPORT

Repository: `github.com/EnJirad/Toner`
Before SHA: `79192231d00c81ad94cd7ae6099134f7094945b3`
After SHA: unchanged commit; all work is in the working tree. No commit was
created because Freebuff's Changes panel owns commits/pushes.

## Architecture

- `toner.py` reduced from 1200 lines to 137: configuration load, service wiring,
  CLI dispatch, shutdown only.
- `core/` — orchestrator, planner, task runner, builder, reviewer, QA, verifier,
  model router, context manager, configuration, text utilities, stop control.
- `tools/` — filesystem, shell, git, search behind one registry with one schema
  and one dispatch table.
- `state/` — task ledger, run/session store, Git-SHA checkpoints, evidence,
  failure manager.
- `policy/` — path containment, secret protection/redaction, command screening,
  approvals.
- `events/` — internal event bus with JSONL observability.
- `toner_tools.AgentTools` remains a working compatibility facade (direct
  methods and `execute`), so external callers and prior scripts keep working.

Explicit pipeline: `QUEUED → BUILDING → REVIEWING → TESTING → VERIFYING →
COMMITTING → DONE`, with `FAILED`, `RETRYING`, `BLOCKED`, `PAUSED`, `CANCELLED`
and validated transitions.

## Implemented

- Persistent task ledger (`TASK-000001.json`) and run ledger (`RUN-000001.json`).
- GitHub-SHA checkpoints before/after risky work plus per-task latest pointer.
- Evidence per task: `build.json`, `diff.json`, `review.json`, `tests.json`,
  `verification.json`, attempt snapshots and failure records, all redacted.
- Builder role: tool loop, no commit, no DONE, records real tool exits.
- Reviewer role: sees the real diff and executed exit codes, returns structured
  decision/issues/severity.
- QA: executes real commands with exit codes, stdout, stderr, duration,
  timeouts; skipped checks never count as passes.
- Verifier: only component that can authorise DONE; requires executed passing
  checks, a clean review and every machine-checkable criterion.
- Failure manager: fingerprints (type/file/line/message), classification
  (transient/recoverable/permanent/dangerous), attempt budget, identical-failure
  block, persisted history.
- Retry policy with backoff; blocked tasks stop their dependants while
  independent tasks continue.
- Risk-aware batching (`low 20`, `medium 5`, `high 1` defaults).
- Model abstraction + role router; one model may serve every role. Only the
  OpenAI-compatible provider ships.
- Context compaction with model summary or explicit fallback; task, rules and
  recent exchanges preserved.
- Event bus covering task/build/review/test/verification/checkpoint/commit and
  agent lifecycle events.
- `/auto`, `/resume`, `/status`, `/report`, `/check`, `/help`, `/exit` preserved.
- Legacy `auto_state.json` imported once; old plans resume with no machine checks
  and are marked accordingly.
- Config centralised with defaults, environment overrides and no new dependency.
- Fixed the pre-existing critical defect: `run_command` always failed because
  `command_timeout` was never set. It now executes commands, returns real exit
  codes and kills timed-out process groups.

## Tests

Executed in this environment (Python 3.10.12):

| Command | Result |
| --- | --- |
| `python3 -m compileall -q toner.py core policy tools state events tests` | exit 0 |
| `python3 -m unittest discover -s tests/unit -t .` | 101 tests, OK, exit 0 |
| `python3 -m unittest discover -s tests/golden -t .` | 14 tests, OK, exit 0 |
| `python3 -m unittest discover -s tests/integration -t .` | 9 tests, OK, exit 0 |
| `python3 -m unittest discover -s tests -t .` | **124 tests, OK, exit 0** |
| `git diff --check` | exit 0 |

Coverage includes safety, tool dispatch, shell timeouts, state persistence,
failure budgets, verifier gating, orchestration, blocking, commits, resume,
planning, reports and the CLI.

No live API test was run: `requests` is not installed here and no API key was
available. Model interactions were tested with a scripted provider. This
distinction is deliberate and not a claim of live-provider verification.

## Golden Tests

Protected behaviours that must not regress:

- path traversal, symlink escape, absolute paths, `.git`/state access
- secret-file read/write denial, backup isolation, redaction
- dangerous-command blocking, command timeout kill, environment filtering
- exact-one-match edits, atomic writes, backups, size limits, deletion gate
- task/checkpoint/evidence persistence across fresh store instances
- failure fingerprinting, identical-failure blocking, attempt budget
- schema/dispatch parity; no destructive Git API exists
- commit requires a message, stages only given files, refuses unrelated staged
  changes, handles Thai/Unicode paths
- only executed evidence can produce DONE; a failed golden check forbids DONE

## Security

Preserved: containment, secret blocking, backup isolation, dangerous command
patterns, environment filtering, interactive approvals, atomic writes with
backups, deletion approval, plan-change refusal.

Strengthened: secret-value redaction in tool output, events and evidence;
`.git` and Toner state blocked from tools; QA limited to allowlisted simple
commands with traversal/absolute-path rejection; commits scoped to task files;
unrelated staged files refused; prompt-injection warning retained in rules.

## Recovery

- State written before important transitions.
- `/resume` validates the ledger, refuses a changed plan, shows task ID, status,
  phase, attempts, last error, failure fingerprint, last known good commit,
  current commit and last test totals.
- Unclean previous runs are recorded as `abandoned`; clean exits mark the active
  run paused.
- Ctrl+C is cooperative: the current step finishes, state is saved, `/resume`
  continues.
- Legacy state is imported without overwriting it.

## Known Limitations

- No live provider call was executed here (no key, no `requests`).
- Only the OpenAI-compatible provider is implemented; others need an adapter.
- QA runs only allowlisted check commands automatically; anything else is
  skipped and blocks automated verification.
- `/check` is intentionally an ad-hoc build without verification; `/auto` is the
  evidence-gated path.
- Git is optional, but without it there is no reviewable diff or commit.
- Deterministic verification evaluates declared machine checks; a task with no
  machine checks can never reach DONE on its own.
- `core/orchestrator.py` is the largest module (844 lines) because it also owns
  the human-readable status/report output. Roles, tools and state are separated.
- Termux itself was not available; all work ran on Linux with Python 3.10.

## Blocked Items

- Live API integration test: blocked by missing credentials/dependency.
- Real target project (`/storage/emulated/0/VNE/Vel/velnox`): not present here;
  the shipped `settings.json` was left unchanged.
- Commits: intentionally not created; Freebuff's Changes panel owns delivery.

## Next Recommended Step

Run a real `/auto` on a small Git repository with `AIUNLIMIT_API_KEY` set and a
plan containing explicit `checks`, then inspect
`.toner/evidence/<task>/verification.json`. If the provider is not
OpenAI-compatible, add a `ModelProvider` adapter next.
