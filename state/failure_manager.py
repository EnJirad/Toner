"""Failure fingerprinting, classification and retry budgeting.

A task may only be retried while its failure budget allows it, it is not a
security or permanent failure, and the same failure has not repeated beyond
the configured limit. Identical failures therefore block instead of looping.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path

from core.textutil import utc_now

_ANSIE_RE = re.compile(r"\x1b\[[0-9;]*m")
_FILE_LINE_RE = re.compile(r'File "([^"]+)",\s+line (\d+)')
_EXCEPTION_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout|Exit))\b:?\s*(.*)"
)

DANGEROUS_MARKERS = (
    "dangerous_command",
    "secret_file",
    "path_outside_root",
    "security violation",
    "blocked a high-risk command",
    "attempted to access a secret",
)

PERMANENT_MARKERS = (
    "modulenotfounderror",
    "no module named",
    "command not found",
    "not installed",
    "unsupported provider",
    "invalid api key",
    "missing api key",
    "invalid configuration",
    "project_root",
    "does not exist",
    "http 400",
    "http 401",
    "http 403",
)

TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connectionerror",
    "connection refused",
    "connection reset",
    "temporarily unavailable",
    "temporary failure",
    "network is unreachable",
    "rate limit",
    "too many requests",
    "overloaded",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


class FailureType(str, Enum):
    TRANSIENT = "TRANSIENT"
    RECOVERABLE = "RECOVERABLE"
    PERMANENT = "PERMANENT"
    DANGEROUS = "DANGEROUS"


@dataclass
class Failure:
    message: str
    phase: str = ""
    kind: str = ""
    file: str = ""
    line: int = 0
    exit_code: int | None = None
    signature: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class FailureDecision:
    action: str
    classification: str
    reason: str
    signature: str
    identical_count: int
    attempts: int
    delay: float = 0.0
    type: str = ""
    file: str = ""
    line: int = 0


def normalise_message(message, limit=240):
    text = _ANSIE_RE.sub("", str(message or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class FailureManager:
    def __init__(
        self,
        directory,
        bus=None,
        max_attempts=3,
        max_identical=2,
        backoff_seconds=2,
        backoff_max_seconds=30,
        redactor=None,
    ):
        self.directory = Path(directory)
        self.redactor = redactor
        self.directory.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self.max_attempts = max(1, int(max_attempts))
        self.max_identical = max(1, int(max_identical))
        self.backoff_seconds = max(0, int(backoff_seconds))
        self.backoff_max_seconds = max(0, int(backoff_max_seconds))

    # -- fingerprinting --------------------------------------------------

    def describe(self, error_text, kind=""):
        text = _ANSIE_RE.sub("", str(error_text or ""))
        file_name = ""
        line_number = 0

        for match in _FILE_LINE_RE.finditer(text):
            file_name = match.group(1)
            line_number = int(match.group(2))

        if not kind:
            for match in _EXCEPTION_RE.finditer(text):
                kind = match.group(1)

        return {
            "file": file_name,
            "line": line_number,
            "kind": kind,
        }

    def fingerprint(self, kind, file_name, line_number, message):
        payload = "|".join(
            [
                str(kind or "").strip(),
                str(file_name or "").strip(),
                str(line_number or 0),
                normalise_message(message),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def classify(self, message, kind="", exit_code=None):
        haystack = f"{kind} {message}".lower()

        if any(marker in haystack for marker in DANGEROUS_MARKERS):
            return FailureType.DANGEROUS

        if any(marker in haystack for marker in TRANSIENT_MARKERS):
            return FailureType.TRANSIENT

        if any(marker in haystack for marker in PERMANENT_MARKERS):
            return FailureType.PERMANENT

        return FailureType.RECOVERABLE

    # -- recording -------------------------------------------------------

    def from_exception(self, exc, phase=""):
        message = str(exc) or type(exc).__name__
        kind = type(exc).__name__
        described = self.describe(message, kind=kind)
        return Failure(
            message=message,
            phase=phase,
            kind=described["kind"],
            file=described["file"],
            line=described["line"],
            signature=self.fingerprint(
                described["kind"],
                described["file"],
                described["line"],
                message,
            ),
        )

    def from_text(self, message, phase="", kind="", exit_code=None):
        described = self.describe(message, kind=kind)
        return Failure(
            message=str(message),
            phase=phase,
            kind=described["kind"],
            file=described["file"],
            line=described["line"],
            exit_code=exit_code,
            signature=self.fingerprint(
                described["kind"],
                described["file"],
                described["line"],
                message,
            ),
        )

    def record(self, task, failure):
        if not failure.signature:
            failure.signature = self.fingerprint(
                failure.kind, failure.file, failure.line, failure.message
            )

        history = task.failure_history if isinstance(task.failure_history, list) else []
        identical = 0

        for entry in reversed(history):
            if entry.get("signature") != failure.signature:
                break
            identical += 1

        classification = self.classify(
            failure.message, kind=failure.kind, exit_code=failure.exit_code
        )
        message = normalise_message(failure.message, 600)

        if self.redactor is not None:
            message = self.redactor.redact(message)

        record = {
            "at": utc_now(),
            "type": classification.value,
            "kind": failure.kind,
            "file": failure.file,
            "line": failure.line,
            "signature": failure.signature,
            "message": message,
            "phase": failure.phase,
            "exit_code": failure.exit_code,
        }

        task.record_failure(record)
        attempts = int(task.attempts or 0)
        decision = self._decide(classification, attempts, identical, failure)

        self.persist(task, decision)

        if self.bus is not None:
            self.bus.emit(
                "task.failed",
                task_id=task.id,
                classification=classification.value,
                signature=failure.signature,
                attempt=attempts,
                error=record["message"],
                action=decision.action,
            )

        return decision

    def _decide(self, classification, attempts, identical, failure):
        signature = failure.signature

        def decision(action, reason, delay=0.0):
            return FailureDecision(
                action=action,
                classification=classification.value,
                reason=reason,
                signature=signature,
                identical_count=identical + 1,
                attempts=attempts,
                delay=delay,
                type=classification.value,
                file=failure.file,
                line=failure.line,
            )

        if classification is FailureType.DANGEROUS:
            return decision(
                "block",
                "Security or policy violation; human review is required.",
            )

        if classification is FailureType.PERMANENT:
            return decision(
                "block",
                "Permanent failure; retrying the same operation cannot fix it.",
            )

        if attempts >= self.max_attempts:
            return decision(
                "block",
                f"Attempt budget exhausted ({attempts}/{self.max_attempts}).",
            )

        if identical + 1 >= self.max_identical:
            return decision(
                "block",
                (
                    "Identical failure repeated "
                    f"{identical + 1} times; task blocked instead of looping."
                ),
            )

        if classification is FailureType.TRANSIENT:
            delay = min(
                self.backoff_seconds * (2 ** identical),
                self.backoff_max_seconds,
            )
            return decision(
                "retry",
                f"Transient failure; retrying in {delay}s.",
                delay=float(delay),
            )

        return decision("retry", "Recoverable failure; retrying with updated evidence.")

    # -- persistence -----------------------------------------------------

    def _path(self, task_id):
        return self.directory / f"{task_id}.json"

    def persist(self, task, decision=None):
        path = self._path(task.id)
        previous = {}

        if path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}

        payload = {
            "task_id": task.id,
            "updated_at": utc_now(),
            "max_attempts": self.max_attempts,
            "max_identical": self.max_identical,
            "history": task.failure_history,
            "last_decision": asdict(decision) if decision else previous.get("last_decision", {}),
        }
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(path)
        return payload

    def load(self, task_id):
        path = self._path(task_id)

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
