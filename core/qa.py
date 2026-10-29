"""QA role: execute checks and record exactly what happened.

No check is ever reported as passed without a process exit code. Commands that
are not on the automated-check allowlist are skipped and recorded as skipped,
which cannot satisfy verification.
"""

from __future__ import annotations

import shlex
import sys
import time
from dataclasses import dataclass, field

from core.textutil import truncate_middle


@dataclass
class QACheckResult:
    name: str
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    status: str = "skipped"
    reason: str = ""
    golden: bool = False

    def to_dict(self):
        return {
            "name": self.name,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
            "timed_out": self.timed_out,
            "status": self.status,
            "reason": self.reason,
            "golden": self.golden,
        }


@dataclass
class QAResult:
    passed: bool
    executed: int
    results: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    exit_codes: list = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    golden_failed: bool = False

    def to_dict(self):
        return {
            "passed": self.passed,
            "executed": self.executed,
            "commands": self.commands,
            "exit_codes": self.exit_codes,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": self.duration,
            "golden_failed": self.golden_failed,
            "results": [result.to_dict() for result in self.results],
        }

    def summary(self):
        failed = [item for item in self.results if item.status == "failed"]

        if failed:
            first = failed[0]
            return (
                f"QA failed: {first.command} exited "
                f"{first.exit_code}. {first.stderr or first.stdout}"[:1000]
            )

        return "QA passed"


class QA:
    def __init__(self, settings, registry, bus=None):
        self.settings = settings
        self.registry = registry
        self.bus = bus
        self.shell = registry.shell

    # -- check planning --------------------------------------------------

    def _plan_checks(self, task, changed_files):
        checks = []

        def add(name, command, golden=False):
            if not command:
                return
            if any(existing["command"] == command for existing in checks):
                return
            checks.append({"name": name, "command": command, "golden": golden})

        git_status = self.registry.git.status()

        if git_status.get("returncode") == 0:
            if changed_files:
                scoped = " ".join(
                    shlex.quote(path) for path in changed_files[:20]
                )
                add("git-diff-check", f"git diff --check -- {scoped}")
            else:
                add("git-diff-check", "git diff --check")

        for path in changed_files:
            if not path.endswith(".py"):
                continue

            target = self.registry.root / path

            if not target.is_file():
                continue

            add(
                f"syntax:{path}",
                f"{shlex.quote(sys.executable)} -m py_compile {shlex.quote(path)}",
            )

        for index, check in enumerate(task.checks, 1):
            if not isinstance(check, dict) or check.get("type") != "command":
                continue

            add(
                f"task-check:{index}",
                str(check.get("command", "")).strip(),
                golden=bool(check.get("golden")),
            )

        add("project-test", str(self.settings.get("test_command", "") or "").strip())

        return checks

    # -- execution -------------------------------------------------------

    def run(self, task, changed_files):
        if self.bus is not None:
            self.bus.emit("test.started", task_id=task.id)

        started = time.monotonic()
        planned = self._plan_checks(task, changed_files)
        results = []
        cache_prefix = str(self.registry.state_root / "pycache")

        for check in planned:
            command = check["command"]
            result = QACheckResult(
                name=check["name"],
                command=command,
                golden=check["golden"],
            )

            if not self.registry.policy.is_safe_check(command):
                result.status = "skipped"
                result.reason = (
                    "Not an allowlisted automated check command; run it manually"
                )
                results.append(result)
                continue

            outcome = self.shell.run(
                command,
                extra_env={"PYTHONPYCACHEPREFIX": cache_prefix},
            )

            if outcome.get("cancelled"):
                result.status = "skipped"
                result.reason = "Approval was denied"
            elif outcome.get("code") == "dangerous_command":
                result.status = "skipped"
                result.reason = "Blocked by the command policy"
            elif outcome.get("returncode") is None:
                result.status = "failed"
                result.reason = outcome.get("error", "Command did not run")
                result.exit_code = -1
            else:
                result.exit_code = outcome.get("returncode")
                result.stdout = outcome.get("stdout", "")
                result.stderr = outcome.get("stderr", "")
                result.duration = float(outcome.get("duration") or 0.0)
                result.timed_out = bool(outcome.get("timed_out"))
                result.status = (
                    "passed"
                    if result.exit_code == 0 and not result.timed_out
                    else "failed"
                )
                if result.timed_out:
                    result.reason = outcome.get("error", "Timed out")

            results.append(result)

        executed = [item for item in results if item.status in ("passed", "failed")]
        failed = [item for item in results if item.status == "failed"]
        golden_failed = any(item.golden and item.status == "failed" for item in results)
        limit = int(self.settings.get("max_output_chars", 30000))

        qa = QAResult(
            passed=not failed,
            executed=len(executed),
            results=results,
            commands=[item.command for item in executed],
            exit_codes=[item.exit_code for item in executed],
            stdout=truncate_middle(
                "\n".join(
                    f"$ {item.command}\n{item.stdout}".rstrip()
                    for item in executed
                ),
                limit,
            ),
            stderr=truncate_middle(
                "\n".join(
                    f"$ {item.command}\n{item.stderr}".rstrip()
                    for item in executed
                    if item.stderr
                ),
                limit,
            ),
            duration=round(time.monotonic() - started, 3),
            golden_failed=golden_failed,
        )

        if self.bus is not None:
            event = "test.passed" if qa.passed and executed else "test.failed"
            if not executed and qa.passed:
                event = "test.skipped"
            self.bus.emit(
                event,
                task_id=task.id,
                executed=qa.executed,
                failed=len(failed),
                duration=qa.duration,
            )

        return qa
