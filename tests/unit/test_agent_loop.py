"""AgentLoop safety tests: read-only gating, loop limits, cancellation,
repetition guards and honest reporting of tool results."""

import json
import unittest

from core.agent_loop import READ_ONLY_TOOLS, AgentLoop
from core.interrupts import request_stop, reset_stop
from core.model_router import ModelError, ModelRouter
from tests.fixtures.fake_model import ScriptedProvider, assistant_text, tool_call
from tests.fixtures.workspace import registry_for, settings_for, temp_project


def make_loop(root, responses, **overrides):
    settings = settings_for(root, **overrides)
    registry = registry_for(settings)
    provider = ScriptedProvider(responses)
    router = ModelRouter(settings, provider=provider)
    loop = AgentLoop(settings, router, registry, redactor=registry.policy.redactor)
    return loop, provider, registry


class ReadOnlyGateTests(unittest.TestCase):
    def test_write_tool_is_refused_in_read_only_mode(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            loop, provider, registry = make_loop(
                root,
                [
                    tool_call("write_file", {"path": "evil.txt", "content": "bad"}),
                    assistant_text("Inspected. Nothing to report."),
                ],
            )
            result = loop.run(
                system="You inspect things.",
                prompt="inspect",
                role="inspector",
                allowed_tools=READ_ONLY_TOOLS,
            )

            self.assertFalse((root / "evil.txt").exists())
            self.assertEqual(result.tool_calls, 1)
            self.assertEqual(result.rounds, 2)
            self.assertEqual(
                result.tool_log[0]["result"].get("code"), "tool_not_allowed"
            )
            self.assertTrue(
                any(v.get("code") == "tool_not_allowed" for v in result.violations)
            )
            self.assertIn("Nothing to report", result.final_text)

    def test_read_only_tool_still_works(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            loop, _, _ = make_loop(
                root,
                [
                    tool_call("read_file", {"path": "app.py"}),
                    assistant_text("The file sets x = 1."),
                ],
            )
            result = loop.run(
                system="inspect",
                prompt="inspect",
                role="inspector",
                allowed_tools=READ_ONLY_TOOLS,
            )
            self.assertIn("x = 1", json.dumps(result.tool_log[0]["result"]))
            self.assertEqual(result.final_text, "The file sets x = 1.")

    def test_run_command_is_refused_in_read_only_mode(self):
        with temp_project() as root:
            loop, _, _ = make_loop(
                root,
                [
                    tool_call("run_command", {"command": "echo hi"}),
                    assistant_text("done"),
                ],
            )
            result = loop.run(
                system="inspect",
                prompt="inspect",
                role="inspector",
                allowed_tools=READ_ONLY_TOOLS,
            )
            self.assertEqual(
                result.tool_log[0]["result"].get("code"), "tool_not_allowed"
            )


class HonestResultTests(unittest.TestCase):
    def test_failed_command_exit_code_is_reported_not_hidden(self):
        with temp_project() as root:
            loop, _, _ = make_loop(
                root,
                [
                    tool_call(
                        "run_command",
                        {"command": "python3 -m py_compile no_such_file.py"},
                    ),
                    assistant_text("The compile check failed."),
                ],
            )
            result = loop.run(system="sys", prompt="run a check")
            payload = result.tool_log[0]["result"]

            self.assertNotEqual(payload.get("returncode"), 0)
            self.assertEqual(result.final_text, "The compile check failed.")

    def test_prose_about_tools_is_never_an_executed_call(self):
        with temp_project() as root:
            loop, _, _ = make_loop(
                root,
                [assistant_text("I would now edit main.py and run the tests.")],
            )
            result = loop.run(system="sys", prompt="go")

            self.assertEqual(result.tool_calls, 0)
            self.assertIn("edit main.py", result.final_text)

    def test_model_error_becomes_failed_loop_with_exception(self):
        with temp_project() as root:
            loop, _, _ = make_loop(root, [ModelError("HTTP 500", transient=True)])
            result = loop.run(system="sys", prompt="go")

            self.assertEqual(result.status, "failed")
            self.assertIsInstance(result.exception, ModelError)
            self.assertIn("HTTP 500", result.error)


class LoopLimitTests(unittest.TestCase):
    def test_repeated_identical_calls_are_refused(self):
        with temp_project(files={"a.py": "1\n"}) as root:
            responses = [
                tool_call("read_file", {"path": "a.py"}, call_id=f"c{i}")
                for i in range(5)
            ]
            responses.append(assistant_text("finished"))
            loop, _, _ = make_loop(root, responses)
            result = loop.run(system="sys", prompt="loop", max_rounds=10)

            codes = [entry["result"].get("code") for entry in result.tool_log]
            self.assertIn("repeated_identical_call", codes)
            self.assertLessEqual(result.rounds, 10)

    def test_max_rounds_caps_the_loop(self):
        with temp_project(files={"a.py": "1\n"}) as root:
            responses = [
                tool_call("read_file", {"path": "a.py"}, call_id=f"c{i}")
                for i in range(6)
            ]
            loop, _, _ = make_loop(root, responses)
            result = loop.run(system="sys", prompt="loop", max_rounds=2)

            self.assertEqual(result.rounds, 2)
            self.assertEqual(result.tool_calls, 2)

    def test_cancellation_stops_the_loop_before_any_tool_call(self):
        with temp_project() as root:
            loop, provider, _ = make_loop(
                root, [assistant_text("never reached")]
            )
            request_stop()

            try:
                result = loop.run(system="sys", prompt="go")
            finally:
                reset_stop()

            self.assertEqual(result.status, "stopped")
            self.assertEqual(result.tool_calls, 0)
            self.assertEqual(provider.remaining, 1)


class TextProtocolExecutionTests(unittest.TestCase):
    def test_text_block_tool_call_is_executed(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            block = (
                "```tool_call\n"
                '{"name": "read_file", "arguments": {"path": "app.py"}}\n'
                "```"
            )
            loop, _, _ = make_loop(
                root, [assistant_text(block), assistant_text("Read it.")]
            )
            result = loop.run(system="sys", prompt="read the file")

            self.assertEqual(result.modes[0], "text")
            self.assertEqual(result.tool_calls, 1)
            self.assertIn("Read it.", result.final_text)


if __name__ == "__main__":
    unittest.main()
