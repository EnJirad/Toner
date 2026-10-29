"""Checkpoints: durable snapshots of task state plus the Git position."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.textutil import timestamp_slug, utc_now


def _safe_label(label):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label)).strip("-")
    return cleaned[:60] or "checkpoint"


class CheckpointManager:
    def __init__(self, directory, git, bus=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.git = git
        self.bus = bus

    def git_snapshot(self):
        snapshot = {
            "available": bool(self.git.available()) if self.git else False,
            "sha": "",
            "branch": "",
            "dirty": None,
            "changed_files": [],
        }

        if not snapshot["available"]:
            return snapshot

        sha = self.git.current_sha()
        if sha.get("returncode") == 0:
            snapshot["sha"] = sha.get("sha", "")

        branch = self.git.branch()
        if branch.get("returncode") == 0:
            snapshot["branch"] = branch.get("branch", "")

        snapshot["dirty"] = self.git.is_dirty()

        changed = self.git.changed_files()
        if changed.get("returncode") == 0:
            snapshot["changed_files"] = changed.get("files", [])

        return snapshot

    def create(self, label, task=None, run_id="", phase="", extra=None):
        scope = task.id if task is not None else (run_id or "run")
        scope = _safe_label(scope)
        snapshot = self.git_snapshot()

        payload = {
            "label": label,
            "created_at": utc_now(),
            "scope": scope,
            "run_id": run_id,
            "task_id": task.id if task is not None else "",
            "task_status": task.status if task is not None else "",
            "phase": phase or (task.current_step if task is not None else ""),
            "attempts": task.attempts if task is not None else 0,
            "git": snapshot,
            "files": snapshot.get("changed_files", []),
            "extra": extra or {},
        }

        scope_dir = self.directory / scope
        scope_dir.mkdir(parents=True, exist_ok=True)
        sequence = len(list(scope_dir.glob("*.json"))) + 1
        name = f"{sequence:04d}-{_safe_label(label)}.json"
        path = scope_dir / name

        self._write(path, payload)
        self._write(self.directory / f"{scope}.json", payload)

        if self.bus is not None:
            self.bus.emit(
                "checkpoint.created",
                checkpoint=str(path),
                task_id=payload["task_id"],
                label=label,
                sha=snapshot.get("sha", ""),
            )

        return payload

    @staticmethod
    def _write(path, payload):
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)

    def latest(self, scope):
        path = self.directory / f"{_safe_label(scope)}.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
