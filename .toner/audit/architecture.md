# Toner — Architecture Audit (pre-upgrade)

## Responsibility map (measured from source, not assumed)

| Responsibility | Location at baseline |
| --- | --- |
| Entry point / REPL | `toner.py:main()` |
| Configuration load + validation | `toner.load_settings()` |
| Orchestration | `toner.Toner` (`auto`, `execute_tasks`, `check`, `resume`) |
| API communication | `toner.call_api()` (module-level, `requests.post`) |
| Tool schema | `toner.make_tool_schema()` |
| Tool dispatch | `AgentTools.execute()` **and** a second hard-coded allowlist inside `Toner.run_agent()` |
| Agent/tool loop | `Toner.run_agent()` |
| File editing | `AgentTools.write_file/edit_file/delete_file` |
| Shell execution | `AgentTools.run_command()` (broken, see baseline) |
| Git | `AgentTools.git_status/git_diff` → `_git()` |
| Search | `AgentTools.search_code()` |
| State | `auto_state.json` + `Toner.load_state/save_state` |
| Checkpoints | `Toner.checkpoint()` — a state JSON dump, no Git SHA |
| `/auto` | `Toner.auto()` + `Toner.execute_tasks()` |
| `/resume` | `Toner.resume()` |
| Error handling | broad `except Exception` per tool, `log_error()` to `errors.jsonl` |
| Completion decision | `execute_tasks()`: one `call_api()` review prompt; the model's `decision` string is assigned directly to `task["status"]` |
| Prompting / rules | `Toner.system_prompt()` + `load_global_rules()` + `load_project_rules()` |
| Approvals | `AgentTools.ask()` (`input()`), gated by settings |

## Data flow at baseline

```
main() → Toner.auto()
  → read .toner/files_md/*.md
  → call_api(plan prompt) → JSON task list
  → checkpoint("plan-created") → auto_state.json
  → execute_tasks()
       → checkpoint(before-TASK)
       → run_agent(task prompt)
             → call_api with tools
             → AgentTools.execute() for each tool call
             → append tool results to messages
             → return free-text report
       → call_api(review prompt) → {"decision": "completed|blocked|pending"}
       → task["status"] = decision
       → checkpoint(after-TASK)
```

The only "verification" is the model reading its own narrative. No test command
was ever executed (and could not be, see baseline defect). No diff, exit code,
or file evidence is consulted.

## Persistence layout at baseline

```
<project_root>/.toner/
├── files_md/                  # human-authored plans (input)
├── new_command.md             # human-authored command (input)
└── ai_recheck/
    ├── auto_state.json        # single source of truth for the plan
    ├── progress.jsonl         # append-only event log
    ├── errors.jsonl
    ├── decisions.md           # never written by the code (dead file)
    ├── checkpoints/*.json     # full state dumps
    ├── reports/report-*.md
    └── backups/               # file backups (read-blocked for the agent)
```

No per-task records, no run records, no evidence, no failure fingerprints.

## Task states at baseline

Free-form strings: `pending`, `in_progress`, `completed`, `blocked`, `skipped`,
plus run-level `idle/planned/running/paused/completed/blocked`. There is no
transition validation; the model can write any of them.

## Coupling problems

1. `toner.py` is 1200 lines and owns config, API, prompting, planning,
   execution, state, reporting, and the REPL.
2. The tool contract exists in three places (schema, dispatch map, allowlist).
3. The agent loop is embedded in the orchestration class and cannot be tested
   without an API endpoint.
4. `requests` is imported at module scope, so nothing can be imported offline.
5. Planning, building, reviewing, and completion all use one prompt shape and
   one model with no role separation.
6. State is a single JSON document; concurrent/multi-task bookkeeping is
   impossible and a corrupt file is fatal (`RuntimeError`, by design).

## Behavior that must be preserved

- Interactive approvals (`ask`) and their settings switches.
- Path containment, backup-directory read block, secret-name block.
- Dangerous-command pattern blocking and secret env-var filtering.
- `edit_file` requiring exactly one match.
- Atomic temp-file + replace writes with a backup.
- Plan-fingerprint guard in `/resume`.
- Refusal to silently overwrite a corrupt `auto_state.json`.
- Existing commands and the `.toner/files_md/` + `new_command.md` workflow.
- `from toner_tools import AgentTools` remains a valid import.
