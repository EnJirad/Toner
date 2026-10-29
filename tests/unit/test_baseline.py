"""Task-scoped change attribution: pre-existing user changes must never be
reported as output of the current task."""

import subprocess
import unittest

from state.baseline import ChangeTracker, parse_porcelain
from tests.fixtures.workspace import registry_for, settings_for, temp_project


def git(root, *args):
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True
    )


class PorcelainParsingTests(unittest.TestCase):
    def test_parse_statuses_and_renames(self):
        text = (
            " M app.py\n"
            "?? new.txt\n"
            "A  staged.py\n"
            'R  old.py -> new name.py\n'
            "?? มีไฟล์.txt\n"
        )
        entries = parse_porcelain(text)

        self.assertEqual(entries["app.py"], "M")
        self.assertEqual(entries["new.txt"], "??")
        self.assertEqual(entries["staged.py"], "A")
        self.assertEqual(entries["new name.py"], "R")
        self.assertEqual(entries["มีไฟล์.txt"], "??")


class AttributionTests(unittest.TestCase):
    def tracker(self, root):
        settings = settings_for(root)
        return ChangeTracker(registry_for(settings))

    def test_pre_existing_changes_are_not_attributed_to_the_task(self):
        with temp_project() as root:
            # A tracked user file that is modified *before* the task starts.
            (root / "user_wip.py").write_text("original\n", encoding="utf-8")
            git(root, "add", "user_wip.py")
            git(root, "commit", "-qm", "add user file")
            (root / "user_wip.py").write_text("user work in progress\n", encoding="utf-8")

            tracker = self.tracker(root)
            baseline = tracker.capture()

            self.assertTrue(baseline.git_available)
            self.assertIn("user_wip.py", baseline.entries)

            # The task creates its own file.
            (root / "task_output.py").write_text("task\n", encoding="utf-8")
            report = tracker.classify(baseline, ["task_output.py"])

            self.assertIn("task_output.py", report.changed_by_task)
            self.assertIn("user_wip.py", report.pre_existing)
            self.assertNotIn("user_wip.py", report.changed_by_task)
            self.assertNotIn("user_wip.py", report.unattributed)

    def test_pre_existing_untracked_files_are_classified_separately(self):
        with temp_project() as root:
            (root / "notes.txt").write_text("mine\n", encoding="utf-8")
            tracker = self.tracker(root)
            baseline = tracker.capture()

            report = tracker.classify(baseline, [])
            self.assertIn("notes.txt", report.pre_existing_untracked)

    def test_unknown_new_file_is_unattributed_not_task_output(self):
        with temp_project() as root:
            tracker = self.tracker(root)
            baseline = tracker.capture()

            # Something changed the project without Toner's file tools.
            (root / "mystery.log").write_text("side effect\n", encoding="utf-8")
            report = tracker.classify(baseline, touched_paths=[])

            self.assertIn("mystery.log", report.unattributed)
            self.assertNotIn("mystery.log", report.changed_by_task)

    def test_unicode_paths_are_attributed(self):
        with temp_project() as root:
            tracker = self.tracker(root)
            baseline = tracker.capture()

            path = "ไฟล์ทดสอบ.py"
            (root / path).write_text("print(1)\n", encoding="utf-8")
            report = tracker.classify(baseline, [path])

            self.assertIn(path, report.changed_by_task)

    def test_report_lines_never_claim_unknown_changes(self):
        with temp_project() as root:
            (root / "user.py").write_text("u\n", encoding="utf-8")
            tracker = self.tracker(root)
            baseline = tracker.capture()
            (root / "other.log").write_text("x\n", encoding="utf-8")
            report = tracker.classify(baseline, [])
            text = "\n".join(report.lines())

            self.assertIn("user.py", text)
            self.assertIn("other.log", text)
            self.assertIn("cannot be attributed", text)
            # The untracked user file must be labelled as pre-existing.
            self.assertIn(
                "Untracked files that existed before this task", text
            )

    def test_git_unavailable_fallback_states_its_limitation(self):
        with temp_project(git=False) as root:
            settings = settings_for(root)
            registry = registry_for(settings)
            tracker = ChangeTracker(registry)
            baseline = tracker.capture()

            self.assertFalse(baseline.git_available)
            report = tracker.classify(baseline, ["only_this.txt"])

            self.assertFalse(report.git_available)
            self.assertEqual(report.changed_by_task, ["only_this.txt"])
            self.assertTrue(
                any("Git was unavailable" in note for note in report.limitations)
            )


if __name__ == "__main__":
    unittest.main()
