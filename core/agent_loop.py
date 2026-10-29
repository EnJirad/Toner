"""One controlled tool-calling loop shared by every agent role.

Used by the Builder (full coding authority, subject to the security policy)
and by read-only inspection, which passes an ``allowed_tools`` allowlist so a
misbehaving model physically cannot modify the project during a chat or
inspection turn.

Loop safety:
- iteration cap (``max_tool_rounds``) — no infinite loops;
- repeated-identical-call cap — no runaway retries of the same failing call;
- cancellation checks every round and before every tool call;
- every tool result is redacted and truncated before it reaches the model;
- tool calls are validated against the registry schema (native or the text
  protocol), so prose about tools is never treated as an executed call.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from core.interrupts import stop_requested
from core.textutil import truncate_middle
from core.toolcall import normalize_message

MAX_REPEAT_IDENTICAL = 3
TOOL_RESULT_CHARS = 8000

READ_ONLY_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "search_code",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
    }
)

TEXT_PROTOCOL_NOTE = (
    "\n\nTool use protocol:\n"
    "- Preferred: return native tool_calls in the OpenAI Chat format.\n"
    "- If your provider does not deliver native tool calls, emit exactly one "
    "fenced block per call:\n"
    "```tool_call\n"
    '{"name": "<tool_name>", "arguments": {...}}\n'
    "```\n"
    "- Never describe a tool call in prose without using one of these forms; "
    "described-but-not-emitted calls will not run.\n"
    "- When you are done, reply with plain text (no tool block)."
)


@dataclass
class LoopResult:
    status: str = "completed"  # completed | stopped | failed
    final_text: str = ""
    tool_calls: int = 0
    rounds: int = 0
    modes: list = field(default_factory=list)
    tool_log: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    context_events: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    error: str = ""
    exception: object = None
    duration: float = 0.0


class AgentLoop:
    def __init__(self, settings, router, registry, bus=None, context=None, redactor=None):
        self.settings = settings
        self.router = router
        self.registry = registry
        self.bus = bus
        self.context = context
        self.redactor = redactor

    def run(
        self,
        system,
        prompt,
        role="builder",
        allowed_tools=None,
        max_rounds=None,
        history=None,
        temperature=0.1,
    ):
        started = time.monotonic()
        allowed = set(allowed_tools) if allowed_tools is not None else None
        default_rounds = max(1, int(self.settings.get("max_tool_rounds", 50)))
        rounds_cap = default_rounds if max_rounds is None else max(
            1, min(default_rounds, int(max_rounds))
        )

        system_text = str(system or "")

        if allowed is not None:
            system_text += (
                "\n\nThis mode is read-only. Only these tools may be called: "
                + ", ".join(sorted(allowed))
                + ". Any other tool call will be refused."
            )

        system_text += TEXT_PROTOCOL_NOTE

        messages = [{"role": "system", "content": system_text}]
        messages.extend(list(history or []))
        messages.append({"role": "user", "content": str(prompt or "")})

        result = LoopResult()
        seen = {}
        final_text = ""
        stopped = False

        for round_number in range(1, rounds_cap + 1):
            result.rounds = round_number

            if stop_requested():
                stopped = True
                break

            try:
                message = self.router.chat(
                    role,
                    messages,
                    tools=self.registry.schemas,
                    temperature=temperature,
                )
            except Exception as exc:
                result.status = "failed"
                result.error = str(exc)
                result.exception = exc
                break

            normalized = normalize_message(message, self.registry.schemas)
            result.modes.append(normalized.mode)
            result.errors.extend(normalized.errors)

            calls = normalized.calls

            if not calls:
                final_text = normalized.content
                break

            assistant = {"role": "assistant", "content": normalized.content or None}

            if normalized.mode == "native":
                assistant["tool_calls"] = calls

            messages.append(assistant)

            for call in calls:
                if stop_requested():
                    stopped = True
                    break

                function = call.get("function") or {}
                name = str(function.get("name") or "")
                raw_args = function.get("arguments") or "{}"

                try:
                    arguments = json.loads(raw_args)
                    arguments = arguments if isinstance(arguments, dict) else None
                except ValueError:
                    arguments = None

                if arguments is None:
                    payload = {
                        "error": "Tool arguments were not a JSON object",
                        "code": "invalid_arguments",
                    }
                elif allowed is not None and name not in allowed:
                    payload = {
                        "error": f"Tool {name!r} is not allowed in this mode",
                        "code": "tool_not_allowed",
                    }
                    result.violations.append(
                        {"tool": name, "code": "tool_not_allowed"}
                    )
                else:
                    signature = json.dumps(
                        [name, arguments], sort_keys=True, ensure_ascii=False
                    )
                    seen[signature] = seen.get(signature, 0) + 1

                    if seen[signature] > MAX_REPEAT_IDENTICAL:
                        payload = {
                            "error": (
                                "The same tool call was repeated "
                                f"{seen[signature]} times without a different "
                                "result; refusing to loop"
                            ),
                            "code": "repeated_identical_call",
                        }
                    else:
                        payload = self.registry.execute(name, arguments)

                result.tool_calls += 1
                result.tool_log.append(
                    {"name": name, "arguments": arguments, "result": payload}
                )

                if isinstance(payload, dict) and payload.get("code") in (
                    "dangerous_command",
                    "secret_file",
                    "path_outside_root",
                    "absolute_path",
                    "backup_access",
                    "git_internal",
                    "state_access",
                ):
                    result.violations.append(
                        {
                            "tool": name,
                            "code": payload.get("code"),
                            "error": str(payload.get("error", ""))[:300],
                        }
                    )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": truncate_middle(
                            self._redact(payload), TOOL_RESULT_CHARS
                        ),
                    }
                )

            if self.context is not None:
                messages, info = self.context.compact(messages, role=role)

                if info.get("compacted"):
                    result.context_events.append(info)

            if stopped:
                break

        if stopped:
            result.status = "stopped"
            result.final_text = ""
        elif result.status != "failed":
            result.final_text = str(final_text or "").strip()

            if not result.final_text and result.tool_calls == 0:
                result.status = "failed"
                result.error = "The model produced neither tool calls nor a reply."

        result.duration = round(time.monotonic() - started, 3)
        return result

    def _redact(self, payload):
        text = json.dumps(payload, ensure_ascii=False, default=str)

        if self.redactor is not None:
            text = self.redactor.redact(text)

        return text
