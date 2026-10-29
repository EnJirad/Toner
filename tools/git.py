"""Git integration.

Only inspection operations and an explicit, message-required commit are
exposed. Destructive operations (reset --hard, clean -f, force push, branch
deletion) are deliberately absent from this API.
"""

from __future__ import annotations

import shutil
import subprocess


class GitTools:
    def __init__(self, root, timeout=30):
        self.root = str(root)
        self.timeout = timeout

    def available(self):
        return shutil.which("git") is not None

    def _run(self, args):
        if not self.available():
            return {"error": "git was not found on PATH", "code": "git_unavailable"}

        try:
            result = subprocess.run(
                ["git", "-c", "core.quotePath=false", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            return {"error": "git command timed out", "code": "git_timeout"}
        except OSError as exc:
            return {"error": str(exc), "code": "git_failed"}

        return {
            "returncode": result.returncode,
            "output": (result.stdout or "").rstrip(),
            "error": (result.stderr or "").rstrip(),
        }

    # -- reads -----------------------------------------------------------

    def status(self):
        return self._run(["status", "--short", "--branch"])

    def diff(self):
        return self._run(["diff", "--"])

    def diff_staged(self):
        return self._run(["diff", "--cached", "--"])

    def current_sha(self):
        result = self._run(["rev-parse", "HEAD"])

        if result.get("returncode") == 0:
            result["sha"] = result["output"].strip()

        return result

    def branch(self):
        result = self._run(["rev-parse", "--abbrev-ref", "HEAD"])

        if result.get("returncode") == 0:
            result["branch"] = result["output"].strip()

        return result

    def log(self, limit=10):
        return self._run(["log", f"-{max(1, int(limit))}", "--oneline"])

    def porcelain(self):
        """Raw ``git status --porcelain`` for baseline/change attribution."""
        return self._run(["status", "--porcelain"])

    def changed_files(self):
        """Return currently changed paths (staged, unstaged and untracked)."""
        result = self._run(["status", "--porcelain"])

        if result.get("returncode") != 0:
            return result

        files = []

        for line in result.get("output", "").splitlines():
            if len(line) < 4:
                continue

            entry = line[3:].strip()

            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]

            entry = entry.strip().strip('"')

            if entry:
                files.append(entry)

        result["files"] = sorted(set(files))
        return result

    def is_dirty(self):
        result = self._run(["status", "--porcelain"])

        if result.get("returncode") != 0:
            return None

        return bool(result.get("output", "").strip())

    # -- the only write operation ---------------------------------------

    def commit(self, message, add_all=True, files=None):
        """Stage explicit files (or everything) and commit.

        ``files`` is the safe path: only those paths are staged, so Toner state
        and unrelated user changes cannot be swept into the commit.
        """
        if not isinstance(message, str) or not message.strip():
            return {"error": "commit message is required", "code": "commit_message_required"}

        if files is not None:
            paths = [str(item) for item in files if str(item).strip()]

            if not paths:
                return {
                    "error": "No changed files were provided for the commit",
                    "code": "nothing_to_commit",
                }

            staged = self._run(["add", "-A", "--", *paths])
        elif add_all:
            staged = self._run(["add", "-A"])
        else:
            staged = {"returncode": 0, "output": "", "error": ""}

        if staged.get("returncode") != 0:
            return {
                "error": staged.get("error") or "git add failed",
                "code": "commit_failed",
                "output": staged.get("output", ""),
            }

        if files is not None:
            cached = self._run(["diff", "--cached", "--name-only"])
            staged_names = set(
                line.strip()
                for line in cached.get("output", "").splitlines()
                if line.strip()
            )
            unrelated = staged_names - set(files)

            if unrelated:
                return {
                    "error": (
                        "Refusing to commit: unrelated staged files exist: "
                        + ", ".join(sorted(unrelated)[:10])
                    ),
                    "code": "unrelated_staged_changes",
                }

        result = self._run(["commit", "-m", message.strip()])

        if result.get("returncode") == 0:
            sha = self.current_sha()
            result["sha"] = sha.get("sha", "")

        return result
