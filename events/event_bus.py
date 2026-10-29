"""Internal, provider-independent event bus.

Events are the integration point for observability now and for future
adapters (Telegram, dashboards, webhooks). Every event is written to a JSONL
log so a crashed run can still be diagnosed.
"""

from __future__ import annotations

import fnmatch
import json
import sys
from pathlib import Path

from core.textutil import json_safe, utc_now


class EventBus:
    def __init__(self, log_path=None, echo=False):
        self.log_path = Path(log_path) if log_path else None
        self.echo = echo
        self._subscribers = []
        self._sequence = 0

        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def subscribe(self, pattern, handler):
        """Subscribe to an exact name or a glob such as ``task.*``."""
        self._subscribers.append((pattern, handler))
        return handler

    def emit(self, event_name, **payload):
        self._sequence += 1
        event = {
            "seq": self._sequence,
            "time": utc_now(),
            "event": event_name,
            "data": json_safe(payload),
        }

        if self.log_path is not None:
            try:
                with self.log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError as exc:
                print(f"Event log write failed: {exc}", file=sys.stderr)

        for pattern, handler in list(self._subscribers):
            if not (
                pattern == event_name
                or fnmatch.fnmatchcase(event_name, pattern)
            ):
                continue

            try:
                handler(event)
            except Exception as exc:  # a subscriber must not break a run
                print(
                    f"Event subscriber failed for {event_name}: {exc}",
                    file=sys.stderr,
                )

        if self.echo:
            print(f"[{event_name}] {json.dumps(event['data'], ensure_ascii=False)}")

        return event

    def count(self):
        return self._sequence
