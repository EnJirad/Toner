# Toner — Test Baseline (pre-upgrade)

Everything below was actually executed in this environment on 2026-10-29.

## Commands and real results

| Command | Result |
| --- | --- |
| `python3 --version` | `Python 3.10.12` |
| `python3 -c "import requests"` | **FAIL** — `ModuleNotFoundError: No module named 'requests'` |
| `python3 -m pytest --version` | **FAIL** — `No module named pytest` |
| `git --version` | `git version 2.34.1` |
| `git rev-parse HEAD` | `79192231d00c81ad94cd7ae6099134f7094945b3` |
| `git status --porcelain` | empty (clean) |
| `python3 -m py_compile toner.py toner_tools.py` | **PASS** |
| `echo "/exit" \| python3 toner.py` | **FAIL** — `ModuleNotFoundError: No module named 'requests'` at import |
| `AgentTools(settings).run_command("echo hello")` | **FAIL** — `{"error": "'AgentTools' object has no attribute 'command_timeout'"}` |
| `AgentTools(settings).path("../../etc/passwd")` | **Blocked** — `ปฏิเสธ path ที่อยู่นอก project_root` |
| `AgentTools(settings).path(".env")` | **Blocked** — `ปฏิเสธการเข้าถึงไฟล์ความลับ` |
| `AgentTools(settings).git_status()` in a non-repo | `{"returncode": 128, "error": "fatal: not a git repository ..."}` (handled, no crash) |

## What this means

- **Syntax baseline: green.** Both modules compile on Python 3.10.
- **Runtime baseline: red.** The program cannot start here (missing dependency,
  missing Termux project root, missing API key), and shell execution is broken
  even when it does start.
- **Regression baseline: absent.** There is no test suite, so "preserving
  behavior" can only be proven by new tests written against measured behavior.

## Pre-refactor test inventory

```
tests/ ................ does not exist
pytest / unittest ..... none configured
CI .................... none in repository
```

## Acceptance gate for this upgrade

A change may only be reported as working when:

1. `python3 -m compileall -q .` passes for all new/modified Python files.
2. `python3 -m unittest discover -s tests -t .` runs and passes.
3. The safety behaviors measured above still hold (covered by golden tests).
4. `run_command` executes a real command with a real exit code (fixes R1).
5. No test result is reported that was not produced by an executed command.
