"""Evidence store: durable, redacted proof for every verification decision."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from core.textutil import json_safe, utc_now
from policy.security import SECRET_ENV_WORDS, SecretRedactor

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class EvidenceStore:
    def __init__(self, root, directory, redactor=None, bus=None):
        self.root = Path(root)
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or SecretRedactor()
        self.bus = bus

    def task_dir(self, task_id):
        if not _SAFE_ID.match(str(task_id)):
            raise ValueError(f"Unsafe task id for evidence directory: {task_id!r}")
        path = self.directory / str(task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sanitise(self, value, key=""):
        key_upper = str(key).upper()

        if key_upper and any(word in key_upper for word in SECRET_ENV_WORDS):
            return "[REDACTED]"

        if isinstance(value, str):
            return self.redactor.redact(value)

        if isinstance(value, dict):
            return {
                str(item_key): self._sanitise(item_value, item_key)
                for item_key, item_value in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [self._sanitise(item) for item in value]

        if isinstance(value, (int, float, bool)) or value is None:
            return value

        return self.redactor.redact(str(value))

    def write(self, task_id, name, payload):
        """Write a JSON evidence file and return its project-relative path."""
        directory = self.task_dir(task_id)
        path = directory / f"{name}.json"
        document = {
            "task_id": str(task_id),
            "name": name,
            "written_at": utc_now(),
            "data": self._sanitise(json_safe(payload)),
        }
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(document, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)

        if self.bus is not None:
            self.bus.emit("evidence.written", task_id=str(task_id), name=name)

        return str(path.relative_to(self.root))

    def write_text(self, task_id, name, text):
        directory = self.task_dir(task_id)
        path = directory / f"{name}.txt"
        path.write_text(self.redactor.redact(str(text)), encoding="utf-8")
        return str(path.relative_to(self.root))

    def read(self, task_id, name):
        path = self.task_dir(task_id) / f"{name}.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def names(self, task_id):
        directory = self.directory / str(task_id)

        if not directory.is_dir():
            return []

        return sorted(path.stem for path in directory.glob("*.json"))

    def archive(self, label):
        """Move all existing evidence aside; never silently delete history."""
        if not any(self.directory.iterdir()):
            return None

        target = self.directory / "_archive" / label
        target.mkdir(parents=True, exist_ok=True)

        for entry in list(self.directory.iterdir()):
            if entry.name == "_archive":
                continue
            destination = target / entry.name
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            shutil.move(str(entry), str(destination))

        return str(target)
