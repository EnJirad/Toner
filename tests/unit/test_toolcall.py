"""Tool-call normalization: native format, text fallback, and refusal to
pretend that prose describing a tool call is an executed call."""

import unittest

from core.toolcall import (
    normalize_message,
    parse_text_calls,
    schema_index,
    validate_call,
)
from tools.registry import make_tool_schema

SCHEMAS = make_tool_schema()


def native(name, arguments, call_id="call-1"):
    import json

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


class NativeFormatTests(unittest.TestCase):
    def test_native_tool_call_is_normalized(self):
        message = native("read_file", {"path": "app.py"})
        result = normalize_message(message, SCHEMAS)

        self.assertEqual(result.mode, "native")
        self.assertEqual(len(result.calls), 1)
        self.assertEqual(result.calls[0]["function"]["name"], "read_file")
        self.assertEqual(result.errors, [])

    def test_native_call_with_invalid_json_arguments_is_reported(self):
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{not json"},
                }
            ],
        }
        result = normalize_message(message, SCHEMAS)

        self.assertEqual(result.mode, "native")
        self.assertTrue(result.errors)
        # The call still goes to the registry so the model gets a real error.
        self.assertEqual(result.calls[0]["function"]["arguments"], "{}")

    def test_unknown_native_tool_is_flagged(self):
        result = normalize_message(native("launch_missiles", {}), SCHEMAS)
        self.assertTrue(any("unknown tool" in item for item in result.errors))


class TextProtocolTests(unittest.TestCase):
    def test_text_block_is_parsed_and_validated(self):
        content = (
            "Let me look at the file.\n"
            "```tool_call\n"
            '{"name": "read_file", "arguments": {"path": "app.py"}}\n'
            "```\n"
        )
        result = normalize_message({"role": "assistant", "content": content}, SCHEMAS)

        self.assertEqual(result.mode, "text")
        self.assertEqual(result.calls[0]["function"]["name"], "read_file")
        args = result.calls[0]["function"]["arguments"]
        self.assertIn("app.py", args)

    def test_text_block_with_wrong_type_is_rejected(self):
        content = (
            "```tool_call\n"
            '{"name": "read_file", "arguments": {"path": "app.py", "start_line": "first"}}\n'
            "```"
        )
        calls, errors = parse_text_calls(content, schema_index(SCHEMAS))
        self.assertEqual(calls, [])
        self.assertTrue(errors)

    def test_text_block_with_missing_required_argument_is_rejected(self):
        content = (
            "```tool_call\n"
            '{"name": "read_file", "arguments": {}}\n'
            "```"
        )
        calls, errors = parse_text_calls(content, schema_index(SCHEMAS))
        self.assertEqual(calls, [])
        self.assertTrue(any("missing required argument" in e for e in errors))

    def test_text_block_with_broken_json_is_rejected(self):
        content = "```tool_call\n{name: broken\n```"
        calls, errors = parse_text_calls(content, schema_index(SCHEMAS))
        self.assertEqual(calls, [])
        self.assertTrue(errors)


class NoPretendExecutionTests(unittest.TestCase):
    def test_prose_describing_a_tool_call_is_not_a_call(self):
        content = (
            "I would now call read_file with path app.py to inspect the "
            "code, and then run_command to test it."
        )
        result = normalize_message({"role": "assistant", "content": content}, SCHEMAS)

        self.assertEqual(result.mode, "none")
        self.assertEqual(result.calls, [])
        self.assertEqual(result.content, content)

    def test_plain_answer_produces_no_calls(self):
        result = normalize_message(
            {"role": "assistant", "content": "Hello! How can I help?"}, SCHEMAS
        )
        self.assertEqual(result.mode, "none")
        self.assertEqual(result.calls, [])

    def test_message_without_tool_calls_field_is_none_mode(self):
        result = normalize_message({"role": "assistant", "content": "ok"}, SCHEMAS)
        self.assertEqual(result.mode, "none")


class SchemaValidationTests(unittest.TestCase):
    def test_validate_call_accepts_valid_arguments(self):
        index = schema_index(SCHEMAS)
        self.assertIsNone(validate_call("read_file", {"path": "a.py"}, index))

    def test_validate_call_rejects_unexpected_argument(self):
        index = schema_index(SCHEMAS)
        error = validate_call("git_status", {"branch": "main"}, index)
        self.assertIsNotNone(error)

    def test_validate_call_rejects_unknown_tool(self):
        index = schema_index(SCHEMAS)
        self.assertIsNotNone(validate_call("nope", {}, index))


if __name__ == "__main__":
    unittest.main()
