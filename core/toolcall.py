"""Tool-call normalization and validation.

Two clearly defined transport formats are supported:

1. **Native** — the provider returns Chat-Completions style ``tool_calls``
   on the assistant message. This is the preferred path.
2. **Text protocol** — providers that ignore the ``tools`` parameter can
   still drive the loop by emitting a fenced block in their reply::

    ```tool_call
    {"name": "read_file", "arguments": {"path": "app.py"}}
    ```

Both formats are validated against the registry schema before use. Prose
that merely *describes* a tool call is never converted into one: when no
valid call is found the text is treated as the model's final answer, so the
agent can never pretend a tool ran.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

TEXT_BLOCK_RE = re.compile(
    r"```(?:tool_call|tool)\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


@dataclass
class NormalizedCalls:
    calls: list = field(default_factory=list)
    content: str = ""
    mode: str = "none"  # "native" | "text" | "none"
    errors: list = field(default_factory=list)


def schema_index(schemas):
    """Map tool name -> schema from the registry tool list."""
    index = {}

    for item in schemas or []:
        function = (item or {}).get("function") or {}
        name = function.get("name")

        if name:
            index[name] = function

    return index


def _check_type(value, expected):
    if isinstance(expected, list):
        return any(_check_type(value, item) for item in expected)

    if expected == "null":
        return value is None
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)

    return True


def validate_call(name, arguments, index):
    """Return an error string when a call is unusable, else None."""
    function = index.get(name)

    if function is None:
        return f"unknown tool: {name}"

    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"

    parameters = function.get("parameters") or {}
    properties = parameters.get("properties") or {}
    required = parameters.get("required") or []

    for key in required:
        if key not in arguments:
            return f"missing required argument: {key}"

    if parameters.get("additionalProperties") is False:
        for key in arguments:
            if key not in properties:
                return f"unexpected argument: {key}"

    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")

        if expected is not None and not _check_type(value, expected):
            return f"argument {key!r} must be {expected}"

    return None


def parse_text_calls(content, index):
    """Parse and validate ```tool_call``` blocks from model text."""
    calls = []
    errors = []

    for order, match in enumerate(TEXT_BLOCK_RE.finditer(str(content or "")), 1):
        raw = match.group(1).strip()

        try:
            data = json.loads(raw)
        except ValueError:
            errors.append(f"text tool block {order} was not valid JSON")
            continue

        if not isinstance(data, dict) or "name" not in data:
            errors.append(f"text tool block {order} must contain 'name'")
            continue

        name = str(data.get("name") or "")
        arguments = data.get("arguments", {})

        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(arguments) if isinstance(arguments, str) else None
            except ValueError:
                arguments = None

        error = validate_call(name, arguments, index)

        if error:
            errors.append(f"text tool block {order}: {error}")
            continue

        calls.append(
            {
                "id": f"text-{order}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }
        )

    return calls, errors


def normalize_message(message, schemas):
    """Convert an assistant message into validated tool calls.

    Returns :class:`NormalizedCalls`. ``mode`` records which transport was
    used, which is what tests assert when checking provider compatibility.
    """
    if not isinstance(message, dict):
        return NormalizedCalls(content="", mode="none")

    index = schema_index(schemas)
    native = message.get("tool_calls") or []
    content = str(message.get("content") or "")

    if native:
        calls = []
        errors = []

        for order, call in enumerate(native, 1):
            function = (call or {}).get("function") or {}
            name = str(function.get("name") or "")
            raw_args = function.get("arguments")
            arguments = None

            if isinstance(raw_args, dict):
                arguments = raw_args
            else:
                try:
                    parsed = json.loads(raw_args or "{}")
                    arguments = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    arguments = None

            if arguments is None:
                # Feed the error back to the model instead of guessing.
                arguments = {}

            error = validate_call(name, arguments, index)

            if error:
                errors.append(f"native call {order}: {error}")

            calls.append(
                {
                    "id": call.get("id") or f"call-{order}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            )

        return NormalizedCalls(
            calls=calls, content=content, mode="native", errors=errors
        )

    text_calls, errors = parse_text_calls(content, index)

    if text_calls:
        return NormalizedCalls(
            calls=text_calls, content=content, mode="text", errors=errors
        )

    return NormalizedCalls(calls=[], content=content, mode="none", errors=errors)
