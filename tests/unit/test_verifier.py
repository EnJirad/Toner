import unittest

from core.builder import BuildResult
from core.qa import QACheckResult, QAResult
from core.reviewer import ReviewResult
from core.verifier import Verifier
from state.task_store import Task
from tests.fixtures.workspace import registry_for, settings_for, temp_project


def passing_check(command="git diff --check"):
    return QACheckResult(
        name="check",
        command=command,
        exit_code=0,
        status="passed",
    )


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self._context = temp_project(
            files={"a.py": "print(1)\n"},
            git=False,
        )
        self.root = self._context.__enter__()
        self.registry = registry_for(settings_for(self.root))
        self.verifier = Verifier({}, self.registry)

    def tearDown(self):
        self._context.__exit__(None, None, None)

    def task(self, checks=None, require_checks=True):
        return Task(
            id="TASK-000001",
            title="t",
            checks=checks if checks is not None else [{"type": "file_exists", "path": "a.py"}],
            require_checks=require_checks,
        )

    def qa(self, passed=True, executed=1, results=None, golden_failed=False):
        results = results if results is not None else [passing_check()]
        return QAResult(
            passed=passed,
            executed=executed,
            results=results,
            commands=[item.command for item in results],
            exit_codes=[item.exit_code for item in results],
            golden_failed=golden_failed,
        )

    def verify(
        self,
        task=None,
        build=None,
        review=None,
        qa=None,
        changed=None,
    ):
        return self.verifier.verify(
            task or self.task(),
            build or BuildResult(status="implemented", files_changed=["a.py"]),
            review or ReviewResult(decision="approve", severity="none"),
            qa or self.qa(),
            changed if changed is not None else ["a.py"],
            "",
        )

    def test_pass_with_executed_checks(self):
        result = self.verify()
        self.assertEqual(result.decision, "pass")

    def test_review_needs_changes_fails(self):
        result = self.verify(
            review=ReviewResult(
                decision="needs_changes", issues=["bug"], severity="medium"
            )
        )
        self.assertEqual(result.decision, "fail")
        self.assertIn("Reviewer decision", " ".join(result.reasons))

    def test_critical_review_fails_even_if_approved(self):
        result = self.verify(
            review=ReviewResult(decision="approve", severity="critical")
        )
        self.assertEqual(result.decision, "fail")

    def test_no_executed_checks_is_insufficient(self):
        result = self.verify(qa=self.qa(passed=True, executed=0, results=[]))
        self.assertEqual(result.decision, "insufficient")

    def test_failed_qa_is_failure(self):
        failed = QACheckResult(
            name="check",
            command="git diff --check",
            exit_code=1,
            status="failed",
            stderr="whitespace error",
        )
        result = self.verify(qa=self.qa(passed=False, results=[failed]))
        self.assertEqual(result.decision, "fail")

    def test_command_criterion_requires_matching_executed_check(self):
        task = self.task(
            checks=[{"type": "command", "command": "python -m pytest"}]
        )
        result = self.verify(task=task)
        self.assertEqual(result.decision, "insufficient")

    def test_command_criterion_passes_from_qa_result(self):
        command = "python -m pytest"
        task = self.task(checks=[{"type": "command", "command": command}])
        result = self.verify(
            task=task,
            qa=self.qa(results=[passing_check(command)]),
        )
        self.assertEqual(result.decision, "pass")

    def test_file_exists_missing_fails(self):
        task = self.task(checks=[{"type": "file_exists", "path": "missing.py"}])
        result = self.verify(task=task)
        self.assertEqual(result.decision, "fail")

    def test_file_absent_and_text_contains(self):
        task = self.task(
            checks=[
                {"type": "file_absent", "path": "gone.txt"},
                {"type": "text_contains", "path": "a.py", "text": "print"},
            ]
        )
        self.assertEqual(self.verify(task=task).decision, "pass")

        failing = self.task(
            checks=[{"type": "text_contains", "path": "a.py", "text": "absent"}]
        )
        self.assertEqual(self.verify(task=failing).decision, "fail")

    def test_no_checks_requires_checks_is_insufficient(self):
        result = self.verify(task=self.task(checks=[], require_checks=True))
        self.assertEqual(result.decision, "insufficient")

    def test_legacy_task_without_checks_can_pass_with_qa(self):
        result = self.verify(task=self.task(checks=[], require_checks=False))
        self.assertEqual(result.decision, "pass")
        self.assertTrue(result.warnings)

    def test_build_failure_fails(self):
        result = self.verify(build=BuildResult(status="failed", error="boom"))
        self.assertEqual(result.decision, "fail")

    def test_policy_violation_fails(self):
        result = self.verify(
            build=BuildResult(
                status="implemented",
                files_changed=["a.py"],
                policy_violations=[{"code": "dangerous_command"}],
            )
        )
        self.assertEqual(result.decision, "fail")

    def test_touching_toner_state_fails(self):
        result = self.verify(changed=[".toner/tasks/TASK-000001.json", "a.py"])
        self.assertEqual(result.decision, "fail")

    def test_touching_secret_file_fails(self):
        result = self.verify(changed=["secrets.json"])
        self.assertEqual(result.decision, "fail")

    def test_golden_failure_fails(self):
        golden = QACheckResult(
            name="golden",
            command="python -m pytest tests/golden",
            exit_code=1,
            status="failed",
            golden=True,
        )
        result = self.verify(
            qa=self.qa(passed=False, results=[golden], golden_failed=True)
        )
        self.assertEqual(result.decision, "fail")
        self.assertIn("golden", " ".join(result.reasons).lower())


if __name__ == "__main__":
    unittest.main()
