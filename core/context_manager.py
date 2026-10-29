"""Controlled conversation context.

Instead of aborting when the conversation grows, Toner summarizes the middle
of the conversation, keeps the system rules, the original task and the most
recent tool exchange, and records that compaction happened. Nothing important
is dropped silently.
"""

from __future__ import annotations

from core.textutil import truncate_middle


class ContextManager:
    def __init__(
        self,
        max_chars=24000,
        router=None,
        bus=None,
        keep_recent=6,
        redactor=None,
    ):
        self.max_chars = max(2000, int(max_chars))
        self.router = router
        self.bus = bus
        self.keep_recent = max(2, int(keep_recent))
        self.redactor = redactor

    @staticmethod
    def size(messages):
        return sum(len(str(message.get("content") or "")) for message in messages)

    def _protected_indices(self, messages):
        protected = set()

        if messages:
            protected.add(0)

        for index, message in enumerate(messages):
            if message.get("role") == "user":
                protected.add(index)
                break

        for index in range(max(0, len(messages) - self.keep_recent), len(messages)):
            protected.add(index)

        return protected

    def _render(self, messages):
        parts = []

        for message in messages:
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(f"{message.get('role', 'unknown')}: {content.strip()[:1500]}")
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                parts.append(
                    f"assistant called {function.get('name', '?')} "
                    f"with {str(function.get('arguments', ''))[:400]}"
                )

        return "\n".join(parts)

    def _summarise(self, middle, role):
        if self.router is None or not middle:
            return ""

        prompt = (
            "Summarize the work below for an AI software agent that must "
            "continue the same task. Preserve: files changed, commands run and "
            "their exit codes, test results, errors, and unresolved issues. "
            "Be concise and factual. Never invent results.\n\n"
            + self._render(middle)[:12000]
        )

        try:
            message = self.router.chat(
                role,
                [
                    {
                        "role": "system",
                        "content": "You compress agent transcripts without losing evidence.",
                    },
                    {"role": "user", "content": prompt},
                ],
                tools=None,
                temperature=0,
            )
            content = message.get("content") or ""
            return content.strip()[:6000]
        except Exception:
            return ""

    def compact(self, messages, role="planner"):
        """Return (messages, info) with the conversation within the budget."""
        before = self.size(messages)
        info = {
            "compacted": False,
            "method": "",
            "removed": 0,
            "before": before,
            "after": before,
        }

        if before <= self.max_chars or len(messages) <= 3:
            return messages, info

        protected = self._protected_indices(messages)
        middle = [
            message
            for index, message in enumerate(messages)
            if index not in protected
        ]
        head = [messages[index] for index in sorted(protected) if index < len(messages)]

        summary = self._summarise(middle, role)
        method = "summary"

        if summary:
            note = {
                "role": "system",
                "content": "[Summary of earlier work]\n" + summary,
            }
        else:
            method = "drop"
            note = {
                "role": "system",
                "content": (
                    f"[Context compacted: {len(middle)} earlier messages were "
                    "omitted to stay within max_context_chars. The task, rules "
                    "and most recent exchanges were preserved.]"
                ),
            }

        compacted = [messages[0], note] + [m for m in head if m is not messages[0]]

        if self.size(compacted) > self.max_chars:
            compacted = [
                dict(
                    message,
                    content=truncate_middle(
                        str(message.get("content") or ""),
                        max(80, self.max_chars // max(2, len(compacted))),
                    ),
                )
                for message in compacted
            ]

        if self.size(compacted) > self.max_chars:
            compacted = [
                message
                for message in compacted
                if message.get("role") == "system"
            ] + [
                {
                    "role": "user",
                    "content": (
                        "Context was reduced to the rules only. Re-inspect the "
                        "project before continuing."
                    ),
                }
            ]

        after = self.size(compacted)
        info.update(
            {
                "compacted": True,
                "method": method,
                "removed": len(middle),
                "after": after,
            }
        )

        if self.bus is not None:
            self.bus.emit(
                "context.compacted",
                method=method,
                removed=len(middle),
                before=before,
                after=after,
            )

        return compacted, info
