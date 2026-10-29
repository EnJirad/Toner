# Toner — Risk Register (pre-upgrade)

Severity is about impact on the goal "safe, reliable autonomous software
factory", not about code style.

| # | Risk | Severity | Evidence | Consequence |
| --- | --- | --- | --- | --- |
| R1 | `run_command` always raises `AttributeError` (`command_timeout` never set) | **Critical** | reproduced: `{"error": "'AgentTools' object has no attribute 'command_timeout'"}` | No tests, builds, or checks can run. Every "completed" task is unverified. |
| R2 | Completion is decided by the same model that did the work | **Critical** | `execute_tasks()` assigns `task["status"] = review_data["decision"]` | Model self-report is the only success signal; hallucinated success is accepted. |
| R3 | No diff/exit-code/evidence inspection before completion | **Critical** | no evidence code paths exist | Wrong or empty changes can be marked done; regressions pass silently. |
| R4 | Unbounded retries across runs | High | `attempts` incremented, never used as a gate | A stuck task can be resumed forever and repeat the same failure. |
| R5 | Context overflow aborts the round | High | `run_agent()` returns a "context too long" string | Long tasks cannot progress; no summarization or state carry-over. |
| R6 | One blocked task halts the whole plan | High | `self.state["status"] = "blocked"; return` in `execute_tasks()` | Independent safe work stops; no partial progress. |
| R7 | API key required to start the process | High | `load_settings()` raises when key missing | `/status`, `/report`, `/resume` cannot be used offline or during recovery. |
| R8 | `requests` imported at module scope | High | `toner.py:11` | The whole CLI is unusable if the package is missing, even for local state. |
| R9 | Tool schema / dispatch / allowlist duplication | Medium | 3 separate lists | A tool added in one place silently fails or is unreachable. |
| R10 | Output truncation keeps only the tail | Medium | `output[-15000:]` | Root-cause lines (usually first) of test failures are lost. |
| R11 | stdout and stderr merged | Medium | `run_command()` concatenates both | Cannot reason about warnings vs. failures. |
| R12 | Tool output written to `progress.jsonl` unredacted | Medium | `log_progress(..., safe_json(result)[:1000])` | Secrets echoed by a command can persist in logs. |
| R13 | `delete_file` approval branch is duplicated | Low | both branches call the same `ask(..., always=True)` | `require_approval_for_deletions=false` has no effect; behaviour is at least not unsafe. |
| R14 | `current_task` stores a string but is initialised to `0` | Low | `auto()` vs `execute_tasks()` | Confusing status output and state consumers. |
| R15 | `resume()` mutates status in memory without saving | Low | resets `in_progress` → `pending` | A crash after resume can leave inconsistent state. |
| R16 | Git support is read-only status/diff | Medium | `_git()` | No SHA checkpointing, no branch/log awareness, no controlled commit. |
| R17 | Checkpoints store state only, no Git SHA | Medium | `checkpoint()` | Cannot answer "what was the last known good commit?" after a crash. |
| R18 | `.gitignore` is empty | Low | 0-byte file | `__pycache__`, runtime state, and reports are easy to commit by accident. |
| R19 | No tests at all | **Critical** | no test files | No regression protection; this upgrade could silently break safety. |
| R20 | Broad `except Exception` around tool execution | Medium | `execute()` | Programming errors become tool "results"; defects like R1 stay invisible. |

## Explicitly NOT considered risks (and therefore preserved)

- Interactive approval prompts.
- Blocking dangerous shell patterns.
- Blocking secret-named files from read/write.
- Rejecting paths outside `project_root`.
- Refusing to auto-resume when plan files changed.
- No force-push, hard reset, or clean in any code path.

## Upstream constraints

- Termux/Android: stdlib-only core, no new mandatory dependencies, short
  synchronous commands, readable plain-text output.
- One provider today (OpenAI-compatible Chat Completions with tool calling).
  The abstraction must not force additional paid APIs.
