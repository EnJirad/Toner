import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.fixtures.workspace import registry_for, settings_for, temp_project


class ShellToolTests(unittest.TestCase):
    def setUp(self):
        self._context = temp_project(git=False)
        self.root = self._context.__enter__()
        self.settings = settings_for(
            self.root,
            command_timeout=2,
            max_command_output_chars=600,
        )
        self.registry = registry_for(self.settings)

    def tearDown(self):
        self._context.__exit__(None, None, None)

    def test_exit_code_and_streams(self):
        result = self.registry.shell.run(
            "printf 'out'; printf 'err' >&2; exit 3"
        )
        self.assertEqual(result["returncode"], 3)
        self.assertEqual(result["stdout"], "out")
        self.assertEqual(result["stderr"], "err")
        self.assertFalse(result["timed_out"])

    def test_success_exit_zero(self):
        result = self.registry.shell.run("true")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"], "")

    def test_command_runs_in_project_root(self):
        result = self.registry.shell.run("pwd")
        self.assertTrue(
            os.path.samefile(result["stdout"].strip(), str(self.root))
        )

    def test_timeout_kills_the_process(self):
        started = time.monotonic()
        result = self.registry.shell.run("sleep 5", timeout=1)
        elapsed = time.monotonic() - started

        self.assertTrue(result["timed_out"])
        self.assertEqual(result["code"], "command_timeout")
        self.assertLess(elapsed, 4.0)

    def test_dangerous_command_is_blocked(self):
        result = self.registry.shell.run("rm -rf /tmp/toner-nope")
        self.assertEqual(result["code"], "dangerous_command")
        self.assertFalse((Path("/tmp/toner-nope")).exists())

    def test_secret_environment_is_filtered(self):
        with patch.dict(
            os.environ,
            {"MY_SECRET_TOKEN": "s3cr3t-value"},
            clear=False,
        ):
            result = self.registry.shell.run(
                'printf %s "${MY_SECRET_TOKEN:-missing}"'
            )

        self.assertEqual(result["stdout"].strip(), "missing")
        self.assertNotIn("s3cr3t-value", result["output"])

    def test_output_truncation_keeps_head_and_tail(self):
        command = (
            f"{sys.executable} -c "
            "\"print('A' * 2000); print('B' * 2000)\""
        )
        result = self.registry.shell.run(command)
        output = result["output"]

        self.assertIn("truncated", output)
        self.assertTrue(output.startswith("A"))
        self.assertTrue(output.rstrip().endswith("B"))
        self.assertLess(len(output), 1200)

    def test_approval_denial_returns_cancelled(self):
        registry = registry_for(
            settings_for(self.root, require_approval_for_commands=True),
            answer="n",
        )
        result = registry.execute("run_command", {"command": "echo hi"})
        self.assertTrue(result.get("cancelled"))

    def test_dispatch_unknown_tool(self):
        result = self.registry.execute("launch_rocket", {})
        self.assertEqual(result["code"], "unknown_tool")

    def test_dispatch_invalid_arguments(self):
        result = self.registry.execute("read_file", {"wrong": 1})
        self.assertEqual(result["code"], "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
