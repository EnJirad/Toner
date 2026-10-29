"""Persistent task ledger.

Each task is a JSON file under ``.toner/tasks/`` so a task's state, attempts,
failure history and evidence survive process exits. Chat history is never the
source of truth for what Toner is doing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

from core.textutil import utc_now


class TaskStatus(str, Enum):
    QUEUED = "QUEUED"
    PLANNING = "PLANNING"
    BUILDING = "BUILDING"
    REVIEWING = "REVIEWING"
    TESTING = "TESTING"
    VERIFYING = "VERIFYING"
    COMMITTING = "COMMITTING"
    DONE = "DONE"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


SUCCESS_STATES = {TaskStatus.DONE.value, TaskStatus.SKIPPED.value}
TERMINAL_STATES = {
    TaskStatus.DONE.value,
    TaskStatus.SKIPPED.value,
    TaskStatus.CANCELLED.value,
}
ACTIVE_STATES = {
    TaskStatus.PLANNING.value,
    TaskStatus.BUILDING.value,
    TaskStatus.REVIEWING.value,
    TaskStatus.TESTING.value,
    TaskStatus.VERIFYING.value,
    TaskStatus.COMMITTING.value,
    TaskStatus.RETRYING.value,
}

_ANY = set(TaskStatus)

ALLOWED_TRANSITIONS = {
    TaskStatus.QUEUED.value: {
        TaskStatus.PLANNING.value,
        TaskStatus.BUILDING.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.PLANNING.value: {
        TaskStatus.BUILDING.value,
        TaskStatus.RETRYING.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.BUILDING.value: {
        TaskStatus.REVIEWING.value,
        TaskStatus.RETRYING.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.REVIEWING.value: {
        TaskStatus.TESTING.value,
        TaskStatus.RETRYING.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.TESTING.value: {
        TaskStatus.VERIFYING.value,
        TaskStatus.RETRYING.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.VERIFYING.value: {
        TaskStatus.COMMITTING.value,
        TaskStatus.DONE.value,
        TaskStatus.RETRYING.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.COMMITTING.value: {
        TaskStatus.DONE.value,
        TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.RETRYING.value: {
        TaskStatus.BUILDING.value,
        TaskStatus.QUEUED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.FAILED.value,
        TaskStatus.PAUSED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.FAILED.value: {
        TaskStatus.RETRYING.value,
        TaskStatus.QUEUED.value,
        TaskStatus.BLOCKED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.BLOCKED.value: {
        TaskStatus.QUEUED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.PAUSED.value: {
        TaskStatus.QUEUED.value,
        TaskStatus.BUILDING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.DONE.value: set(),
    TaskStatus.SKIPPED.value: set(),
    TaskStatus.CANCELLED.value: set(),
}

LEGACY_STATUS_MAP = {
    "pending": TaskStatus.QUEUED.value,
    "in_progress": TaskStatus.QUEUED.value,
    "completed": TaskStatus.DONE.value,
    "blocked": TaskStatus.BLOCKED.value,
    "skipped": TaskStatus.SKIPPED.value,
}

VALID_RISKS = ("low", "medium", "high")


class InvalidTransition(ValueError):
    pass


@dataclass
class Task:
    id: str
    title: str = ""
    description: str = ""
    status: str = TaskStatus.QUEUED.value
    dependencies: list = field(default_factory=list)
    acceptance_criteria: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    verification: str = ""
    risk: str = "medium"
    require_checks: bool = True
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    attempts: int = 0
    failure_count: int = 0
    current_step: str = ""
    last_error: str = ""
    last_known_good_commit: str = ""
    current_commit: str = ""
    evidence: list = field(default_factory=list)
    failure_history: list = field(default_factory=list)
    result: str = ""
    review: dict = field(default_factory=dict)
    verification_result: dict = field(default_factory=dict)
    last_test: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload = {key: value for key, value in data.items() if key in known}
        payload["status"] = _normalise_status(payload.get("status", TaskStatus.QUEUED.value))
        task = cls(**payload)

        for key in ("dependencies", "acceptance_criteria", "checks", "evidence",
                    "failure_history", "notes"):
            if not isinstance(getattr(task, key), list):
                setattr(task, key, [])

        if task.risk not in VALID_RISKS:
            task.risk = "medium"

        return task

    def transition(self, new_status):
        value = _normalise_status(new_status)

        if value == self.status:
            return self.status

        allowed = ALLOWED_TRANSITIONS.get(self.status, set())

        if value not in allowed:
            raise InvalidTransition(
                f"Task {self.id}: illegal transition {self.status} -> {value}"
            )

        self.status = value
        self.updated_at = utc_now()
        return self.status

    def record_failure(self, record):
        self.failure_history.append(record)
        self.failure_count = int(self.failure_count) + 1
        self.last_error = str(record.get("message", ""))[:4000]
        self.updated_at = utc_now()


def _normalise_status(value):
    value = getattr(value, "value", value)

    if not isinstance(value, str):
        raise InvalidTransition(f"Invalid task status: {value!r}")

    upper = value.upper()

    if upper == "PENDING":
        return TaskStatus.QUEUED.value

    if upper in _ANY:
        return upper

    raise InvalidTransition(f"Unknown task status: {value}")


class TaskStore:
    def __init__(self, directory, bus=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bus = bus

    # -- persistence -----------------------------------------------------

    def _path(self, task_id):
        return self.directory / f"{task_id}.json"

    def all(self):
        tasks = []

        for path in sorted(self.directory.glob("TASK-*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError(
                    f"Task file is unreadable: {path} ({exc})"
                ) from exc

            tasks.append(Task.from_dict(data))

        return tasks

    def get(self, task_id):
        path = self._path(task_id)

        if not path.is_file():
            return None

        try:
            return Task.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def save(self, task, event=None):
        task.updated_at = utc_now()
        payload = task.to_dict()
        path = self._path(task.id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)

        if event and self.bus is not None:
            self.bus.emit(
                event,
                task_id=task.id,
                status=task.status,
                title=task.title,
            )

        return payload

    # -- creation --------------------------------------------------------

    def next_id(self):
        highest = 0

        for path in self.directory.glob("TASK-*.json"):
            stem = path.stem

            try:
                highest = max(highest, int(stem.split("-")[1]))
            except (IndexError, ValueError):
                continue

        return f"TASK-{highest + 1:06d}"

    def add_tasks(self, specs):
        """Create canonical tasks from planner specs, remapping dependencies."""
        created = []
        id_map = {}
        sources = []

        for index, spec in enumerate(specs, 1):
            source_id = str(spec.get("id") or f"T{index:03d}")

            if source_id in id_map:
                raise ValueError(f"Duplicate task id in plan: {source_id}")

            sources.append(source_id)
            id_map[source_id] = self.next_id()
            # Reserve the id so the next lookup does not collide.
            self.save(Task(id=id_map[source_id]))

        for source_id, spec in zip(sources, specs):
            dependencies = [str(dep) for dep in spec.get("dependencies", [])]
            unknown = [dep for dep in dependencies if dep not in id_map]

            if unknown:
                raise ValueError(
                    f"Task {source_id} depends on unknown tasks: {unknown}"
                )

            task = Task(
                id=id_map[source_id],
                title=str(spec.get("title", "") or "").strip(),
                description=str(spec.get("description", "") or "").strip(),
                dependencies=[id_map[dep] for dep in dependencies],
                acceptance_criteria=[
                    str(item)
                    for item in spec.get("acceptance_criteria", [])
                    if isinstance(item, (str, int, float))
                ],
                checks=[
                    item
                    for item in spec.get("checks", [])
                    if isinstance(item, dict)
                ],
                verification=str(spec.get("verification", "") or ""),
                risk=spec.get("risk") if spec.get("risk") in VALID_RISKS else "medium",
                require_checks=bool(spec.get("require_checks", True)),
            )
            self.save(task, event="task.created")
            created.append(task)

        return created

    # -- queries ---------------------------------------------------------

    def counts(self):
        counts = {
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "paused": 0,
            "cancelled": 0,
        }

        for task in self.all():
            if task.status in ACTIVE_STATES:
                counts["running"] += 1
            elif task.status == TaskStatus.QUEUED.value:
                counts["queued"] += 1
            elif task.status in SUCCESS_STATES:
                counts["completed"] += 1
            elif task.status == TaskStatus.FAILED.value:
                counts["failed"] += 1
            elif task.status == TaskStatus.BLOCKED.value:
                counts["blocked"] += 1
            elif task.status == TaskStatus.PAUSED.value:
                counts["paused"] += 1
            elif task.status == TaskStatus.CANCELLED.value:
                counts["cancelled"] += 1

        return counts

    def unfinished(self):
        return [task for task in self.all() if task.status not in SUCCESS_STATES]

    def ready(self):
        tasks = self.all()
        by_id = {task.id: task for task in tasks}
        return [
            task
            for task in tasks
            if task.status == TaskStatus.QUEUED.value
            and all(
                by_id.get(dependency, Task(id=dependency)).status in SUCCESS_STATES
                for dependency in task.dependencies
            )
        ]

    def next_task(self):
        """Return the next ready task, or None."""
        ready = self.ready()
        return ready[0] if ready else None

    def block_dependents(self, blocked_id, reason):
        """Block queued tasks that (transitively) depend on ``blocked_id``."""
        blocked = []
        dead = {blocked_id}
        changed = True

        while changed:
            changed = False

            for task in self.all():
                if task.status != TaskStatus.QUEUED.value:
                    continue
                if not any(dep in dead for dep in task.dependencies):
                    continue

                task.transition(TaskStatus.BLOCKED)
                task.notes.append(reason)
                task.last_error = reason
                self.save(task, event="task.blocked")
                blocked.append(task.id)
                dead.add(task.id)
                changed = True

        return blocked

    # -- maintenance -----------------------------------------------------

    def archive(self, label):
        """Move existing task records aside instead of overwriting them."""
        import shutil

        existing = list(self.directory.glob("TASK-*.json"))

        if not existing:
            return None

        target = self.directory / "_archive" / label
        target.mkdir(parents=True, exist_ok=True)

        for path in existing:
            shutil.move(str(path), str(target / path.name))

        return str(target)

    def import_legacy(self, state_file):
        """Import legacy ``auto_state.json`` tasks once. Returns the count."""
        state_file = Path(state_file)

        if not state_file.is_file() or self.all():
            return 0

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Legacy state is unreadable and was not modified: {state_file} ({exc})"
            ) from exc

        legacy_tasks = state.get("tasks")

        if not isinstance(legacy_tasks, list) or not legacy_tasks:
            return 0

        id_map = {}
        for index, legacy in enumerate(legacy_tasks, 1):
            id_map[str(legacy.get("id", f"T{index:03d}"))] = f"TASK-{index:06d}"

        imported = []

        for legacy in legacy_tasks:
            if not isinstance(legacy, dict):
                continue

            legacy_id = str(legacy.get("id", ""))
            dependencies = [
                id_map[dep]
                for dep in legacy.get("dependencies", [])
                if dep in id_map
            ]
            criteria = legacy.get("acceptance_criteria", [])

            task = Task(
                id=id_map[legacy_id],
                title=str(legacy.get("title", "") or ""),
                description=str(legacy.get("description", "") or ""),
                status=LEGACY_STATUS_MAP.get(
                    str(legacy.get("status", "pending")).lower(),
                    TaskStatus.QUEUED.value,
                ),
                dependencies=dependencies,
                acceptance_criteria=criteria if isinstance(criteria, list) else [],
                checks=[],
                require_checks=False,
                verification=str(legacy.get("verification", "") or ""),
                attempts=int(legacy.get("attempts", 0) or 0),
                result=str(legacy.get("result", "") or ""),
                review=legacy.get("review", {}) if isinstance(legacy.get("review"), dict) else {},
                notes=[
                    "Imported from legacy auto_state.json; "
                    "the original plan had no machine-checkable acceptance checks."
                ],
            )
            self.save(task)
            imported.append(task)

        if imported and self.bus is not None:
            self.bus.emit("state.legacy_imported", count=len(imported))

        return len(imported)
