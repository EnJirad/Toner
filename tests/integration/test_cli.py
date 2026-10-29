import json
import os
import subprocess
import sys
import unittest

from tests.fixtures.workspace import REPO_ROOT, settings_for, temp_project


class CliTests(unittest.TestCase):
    def run_cli(self, root, commands):
        settings = settings_for(root, api_key="")
        settings_path = root / "settings.json"
        settings_path.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("AIUNLIMIT_API_KEY", None)
        env["TONER_SETTINGS"] = str(settings_path)

        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "toner.py")],
            input=commands,
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=90,
        )

    def test_help_status_exit_without_api_key(self):
        with temp_project(git=False) as root:
            result = self.run_cli(root, "/help\n/status\n/exit\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("/auto", result.stdout)
            self.assertIn("TONER STATUS", result.stdout)
            self.assertIn("No API key configured", result.stdout)
            self.assertIn("Toner closed.", result.stdout)

    def test_report_writes_a_real_file(self):
        with temp_project(git=False) as root:
            result = self.run_cli(root, "/report\n/exit\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Report written", result.stdout)

            reports = list(
                (root / ".toner" / "ai_recheck" / "reports").glob("report-*.md")
            )
            self.assertEqual(len(reports), 1)
            self.assertIn(
                "# Toner Engineering Report",
                reports[0].read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
