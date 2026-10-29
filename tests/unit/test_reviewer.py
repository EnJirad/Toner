import unittest

from core.builder import BuildResult
from core.model_router import ModelRouter
from core.reviewer import Reviewer
from state.task_store import Task
from tests.fixtures.fake_model import ScriptedProvider, assistant_text, review_json
from tests.fixtures.workspace import settings_for, temp_dir


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = temp_dir()
        self.root = self._tmp.__enter__()
        self.settings = settings_for(self.root)
        self.task = Task(id="TASK-000001", title="t", description="d")
        self.build = BuildResult(status="implemented", summary="did it")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def review(self, response):
        provider = ScriptedProvider([response])
        reviewer = Reviewer(
            self.settings, ModelRouter(self.settings, provider=provider)
        )
        return reviewer.review(self.task, self.build, "diff --git a/x b/x", ["x"])

    def test_approve(self):
        result = self.review(review_json("approve", [], "none"))
        self.assertEqual(result.decision, "approve")
        self.assertEqual(result.severity, "none")

    def test_unparseable_output_is_needs_changes(self):
        result = self.review(assistant_text("looks fine to me"))
        self.assertEqual(result.decision, "needs_changes")
        self.assertTrue(result.issues)

    def test_invalid_decision_is_needs_changes(self):
        result = self.review(
            assistant_text('{"decision": "lgtm", "issues": [], "severity": "none"}')
        )
        self.assertEqual(result.decision, "needs_changes")

    def test_invalid_severity_defaults_conservatively(self):
        result = self.review(
            assistant_text(
                '{"decision": "reject", "issues": ["bad"], "severity": "apocalyptic"}'
            )
        )
        self.assertEqual(result.decision, "reject")
        self.assertEqual(result.severity, "medium")

    def test_issue_string_becomes_list(self):
        result = self.review(
            assistant_text(
                '{"decision": "needs_changes", "issues": "one issue", "severity": "low"}'
            )
        )
        self.assertEqual(result.issues, ["one issue"])


if __name__ == "__main__":
    unittest.main()
