"""Conversation state shared by chat, inspection and coding flows.

Holds the rolling message history plus the metadata that lets follow-up
messages ("ทำต่อ", "test again") reuse the active task context instead of
starting something new. Pure in-memory state; no terminal or tool access.
"""

from __future__ import annotations

from core.textutil import truncate_middle

MAX_HISTORY_MESSAGES = 40
MAX_ENTRY_CHARS = 4000


class Conversation:
    def __init__(self, max_messages=MAX_HISTORY_MESSAGES, redactor=None):
        self.max_messages = int(max_messages)
        self.redactor = redactor
        self.messages = []
        self.last_intent = ""
        self.last_task_id = ""
        self.last_summary = ""

    # -- history ---------------------------------------------------------

    def add(self, role, content):
        text = str(content or "")

        if self.redactor is not None:
            text = self.redactor.redact(text)

        self.messages.append(
            {"role": role, "content": truncate_middle(text, MAX_ENTRY_CHARS)}
        )

        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages :]

        return self.messages

    def history(self):
        return list(self.messages)

    def prompt_context(self):
        """Compact transcript for the intent classifier."""
        return [
            {"role": item["role"], "content": item["content"]}
            for item in self.messages[-6:]
        ]

    # -- task context ----------------------------------------------------

    def record_result(self, intent, task_id="", summary=""):
        self.last_intent = str(intent or "")
        self.last_task_id = str(task_id or "")
        self.last_summary = truncate_middle(str(summary or ""), 500)

    def active_task_description(self):
        if not self.last_task_id:
            return ""

        return (
            f"{self.last_task_id}: {self.last_summary or self.last_intent}"
        ).strip()
