"""Run and session persistence.

A *run* is one execution of /auto, /check or /resume. A *session* records the
plan fingerprint and run-level status so a restart can answer "what was I
doing?" without replaying chat history.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.textutil import utc_now


class RunStore:
    def __init__(self, runs_directory, state_directory, bus=None):
        self.runs_directory = Path(runs_directory)
        self.state_directory = Path(state_directory)
        self.runs_directory.mkdir(parents=True, exist_ok=True)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.active_file = self.state_directory / "active_run.json"
        self.session_file = self.state_directory / "session.json"
        self.bus = bus

    # -- runs ------------------------------------------------------------

    def _next_id(self):
        highest = 0

        for path in self.runs_directory.glob("RUN-*.json"):
            try:
                highest = max(highest, int(path.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue

        return f"RUN-{highest + 1:06d}"

    def _run_path(self, run_id):
        return self.runs_directory / f"{run_id}.json"

    def start(self, task_ids, kind="auto", session_limit=0, update_session=True):
        run = {
            "id": self._next_id(),
            "kind": kind,
            "status": "running",
            "started_at": utc_now(),
            "finished_at": "",
            "task_ids": list(task_ids),
            "session_limit": int(session_limit),
            "steps": 0,
            "notes": [],
        }
        self.save(run)
        self._write_json(self.active_file, run)

        if update_session:
            self.update_session(
                status="running",
                run_id=run["id"],
                reason="",
            )

        if self.bus is not None:
            self.bus.emit("run.started", run_id=run["id"], kind=kind)

        return run

    def save(self, run):
        self._write_json(self._run_path(run["id"]), run)
        return run

    def finish(self, run, status, note="", update_session=True):
        run["status"] = status
        run["finished_at"] = utc_now()

        if note:
            run["notes"].append(note)

        self.save(run)

        if self.active_file.is_file():
            try:
                self.active_file.unlink()
            except OSError:
                pass

        if update_session:
            self.update_session(status=status, run_id=run["id"], reason=note)

        if self.bus is not None:
            self.bus.emit("run.finished", run_id=run["id"], status=status)

        return run

    def load(self, run_id):
        path = self._run_path(run_id)

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def latest(self):
        paths = sorted(self.runs_directory.glob("RUN-*.json"))

        if not paths:
            return None

        return self.load(paths[-1].stem)

    def active(self):
        if not self.active_file.is_file():
            return None

        try:
            return json.loads(self.active_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    # -- session ---------------------------------------------------------

    def session(self):
        if not self.session_file.is_file():
            return {}

        try:
            return json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def update_session(self, **fields):
        session = self.session()
        session.update(fields)
        session["updated_at"] = utc_now()
        self._write_json(self.session_file, session)
        return session

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _write_json(path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
