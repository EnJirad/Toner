"""Scripted model provider used by unit and integration tests."""

from __future__ import annotations

import json

from core.model_router import ModelError, ModelProvider


class ScriptedProvider(ModelProvider):
    name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    @property
    def remaining(self):
        return len(self.responses)

    def chat(self, messages, tools=None, temperature=0.1, model=None):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": tools,
                "temperature": temperature,
                "model": model,
            }
        )

        if not self.responses:
            raise ModelError("scripted provider exhausted", transient=False)

        item = self.responses.pop(0)

        if isinstance(item, Exception):
            raise item

        if callable(item):
            return item(messages, tools)

        return item


def assistant_text(text):
    return {"role": "assistant", "content": text}


def tool_call(name, arguments, call_id="call-1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def review_json(decision="approve", issues=None, severity="none"):
    return assistant_text(
        json.dumps(
            {
                "decision": decision,
                "issues": issues or [],
                "severity": severity,
            }
        )
    )


def plan_json(tasks):
    return assistant_text(json.dumps({"tasks": tasks}))
