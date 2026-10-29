"""Shell execution with policy screening, timeouts and structured results."""

from __future__ import annotations

import os
import signal
import subprocess
import time

from policy.security import SecurityError
from core.textutil import truncate_middle


class ShellTools:
    def __init__(self, policy, approval):
        self.policy = policy
        self.approval = approval

    def _terminate(self, process):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass

    def run(self, command, timeout=None, auto_approve=False, extra_env=None):
        """Run a screened command inside project_root.

        Returns a structured result. Policy denials include a stable ``code``;
        timeouts set ``timed_out`` and still report a return code.
        """
        try:
            self.policy.screen_command(command)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code, "timed_out": False}

        print(f"\n💻 Command: {command}")

        if not auto_approve and not self.approval.confirm(
            "Allow this command to run?",
            setting="require_approval_for_commands",
        ):
            return {"cancelled": True}

        effective_timeout = int(timeout or self.policy.command_timeout)
        env = self.policy.filter_env()

        for key, value in (extra_env or {}).items():
            env[str(key)] = str(value)
        started = time.monotonic()
        timed_out = False

        try:
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self.policy.root),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return {
                "error": str(exc),
                "code": "spawn_failed",
                "timed_out": False,
            }

        try:
            stdout, stderr = process.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
        except KeyboardInterrupt:
            self._terminate(process)
            try:
                process.communicate(timeout=5)
            except Exception:
                pass
            raise

        duration = round(time.monotonic() - started, 3)
        limit = self.policy.max_command_output_chars
        stdout = truncate_middle(self.policy.redactor.redact(stdout or ""), limit)
        stderr = truncate_middle(self.policy.redactor.redact(stderr or ""), limit)
        combined = (stdout + "\n" + stderr).strip()
        combined = truncate_middle(combined, limit)

        result = {
            "returncode": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "output": combined,
            "timed_out": timed_out,
            "duration": duration,
        }

        if timed_out:
            result["error"] = (
                f"Command exceeded the {effective_timeout}s timeout and was terminated"
            )
            result["code"] = "command_timeout"

        return result
