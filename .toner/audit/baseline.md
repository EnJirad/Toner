# Toner — Baseline (pre-upgrade)

Recorded before any source change.

| Item | Value |
| --- | --- |
| Repository | `github.com/EnJirad/Toner` |
| Branch | `main` |
| Git SHA at baseline | `79192231d00c81ad94cd7ae6099134f7094945b3` |
| Working tree | clean (`git status --porcelain` empty) |
| Python | 3.10.12 |
| Git | 2.34.1 |
| `requests` | **not installed** in the audit environment |
| Test framework | none (no `tests/`, no pytest, no unittest suite) |
| `requirements.txt` | absent |
| `.gitignore` | present but **empty** (0 bytes) |
| `py_compile` | `python3 -m py_compile toner.py toner_tools.py` → **PASS** |

## Files at baseline

```
.gitignore        (empty)
README.md
settings.json
toner.py          1200 lines
toner_rule.md
toner_tools.py     552 lines
```

## How the application actually runs

```bash
python toner.py          # interactive REPL, reads settings.json, imports requests
```

Measured startup in this environment:

```
$ echo "/exit" | python3 toner.py
Traceback (most recent call last):
  File ".../toner.py", line 11, in <module>
    import requests
ModuleNotFoundError: No module named 'requests'
```

So at baseline the entry point cannot start without the optional-at-audit-time
`requests` package. Even with `requests` installed, startup would still fail
because:

1. `settings.json.project_root` is `/storage/emulated/0/VNE/Vel/velnox`
   (Termux path), which does not exist here, and
2. `api_key` is empty and `AIUNLIMIT_API_KEY` is unset.

`load_settings()` requires all three of `api_url`, `model`, `project_root` and a
non-empty API key before any command runs, so even `/help` and `/status` are
unavailable offline.

## Commands at baseline

`/check`, `/auto`, `/status`, `/resume`, `/report`, `/help`, `/exit`, plus plain
text prompts. Interactive approvals use `input()`.

## Measured functional defect (critical)

`AgentTools.__init__` never assigns `command_timeout`, but `run_command()`
passes `timeout=self.command_timeout`. The resulting `AttributeError` is caught
by the broad `except Exception`, converting every shell command into an error:

```
has command_timeout attr: False
💻 คำสั่ง: echo hello
run_command -> {"error": "'AgentTools' object has no attribute 'command_timeout'"}
```

Consequence: **Toner cannot execute any command at baseline**. Tests, builds,
linters, and git-via-command are all impossible, so `/auto` can never verify
anything. This is the single most important pre-existing runtime bug.

## Measured safety behavior that does work

```
traversal blocked -> ปฏิเสธ path ที่อยู่นอก project_root
secret blocked     -> ปฏิเสธการเข้าถึงไฟล์ความลับ
```

Path containment and secret-name blocking are real and must be preserved.

## Known limitations at baseline

- No automated tests of any kind.
- Completion is decided by one model call reviewing the model's own report.
- No task ledger, run ledger, evidence files, or failure fingerprints.
- Checkpoints are copies of `auto_state.json`; no Git SHA is recorded.
- One blocked task stops the whole plan.
- No retry policy; `attempts` is recorded but never used to bound anything.
- Context overflow aborts the round instead of compacting.
- Provider/API/model are hard-coded into `toner.py`.
- `run_command` merges stdout+stderr and keeps only the tail of output.
- Tool schema, dispatch table, and the allowlist in `run_agent` are three
  duplicated lists that can drift.

## Dependency status

Required by README: Python ≥ 3.10, `requests`, an OpenAI-compatible Chat
Completions endpoint with tool calling, and Git. Nothing else is declared or
installed. No dependency was added for this upgrade.
