import unittest

from core.model_router import ModelRouter
from core.planner import Planner, PlannerError
from tests.fixtures.fake_model import ScriptedProvider, plan_json
from tests.fixtures.workspace import settings_for, temp_project


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self._context = temp_project(
            files={
                ".toner/files_md/01-plan.md": "# Plan\nDo the thing.",
                ".toner/files_md/.hidden/skip.md": "hidden",
            },
            git=False,
        )
        self.root = self._context.__enter__()
        self.settings = settings_for(self.root)
        self.plans_dir = self.root / ".toner" / "files_md"

    def tearDown(self):
        self._context.__exit__(None, None, None)

    def test_plan_files_and_fingerprint(self):
        provider = ScriptedProvider([])
        planner = Planner(
            self.settings, ModelRouter(self.settings, provider=provider)
        )

        files = planner.read_plan_files(self.plans_dir)
        self.assertEqual([path.name for path in files], ["01-plan.md"])

        first = planner.fingerprint(files, self.root / ".toner")
        (self.plans_dir / "01-plan.md").write_text("changed", encoding="utf-8")
        second = planner.fingerprint(
            planner.read_plan_files(self.plans_dir), self.root / ".toner"
        )
        self.assertNotEqual(first, second)

    def test_valid_plan_is_normalised(self):
        provider = ScriptedProvider(
            [
                plan_json(
                    [
                        {
                            "id": "T1",
                            "title": "Add login",
                            "description": "Authentication endpoint",
                            "risk": "high",
                            "acceptance_criteria": ["login works"],
                            "checks": [
                                {"type": "file_exists", "path": "auth.py"}
                            ],
                        }
                    ]
                )
            ]
        )
        planner = Planner(
            self.settings, ModelRouter(self.settings, provider=provider)
        )
        tasks = planner.plan("plan text")

        self.assertEqual(tasks[0]["id"], "T1")
        self.assertEqual(tasks[0]["risk"], "high")
        self.assertEqual(tasks[0]["checks"][0]["type"], "file_exists")

    def test_risk_is_inferred_when_missing(self):
        provider = ScriptedProvider(
            [
                plan_json(
                    [
                        {
                            "id": "T1",
                            "title": "Update payment migration",
                            "description": "",
                            "checks": [],
                        }
                    ]
                )
            ]
        )
        tasks = Planner(
            self.settings, ModelRouter(self.settings, provider=provider)
        ).plan("plan")
        self.assertEqual(tasks[0]["risk"], "high")

    def test_invalid_json_rejected(self):
        provider = ScriptedProvider(
            [{"role": "assistant", "content": "not json at all"}]
        )
        planner = Planner(
            self.settings, ModelRouter(self.settings, provider=provider)
        )
        with self.assertRaises(PlannerError):
            planner.plan("plan")

    def test_duplicate_ids_rejected(self):
        provider = ScriptedProvider(
            [
                plan_json(
                    [
                        {"id": "T1", "title": "a", "checks": []},
                        {"id": "T1", "title": "b", "checks": []},
                    ]
                )
            ]
        )
        with self.assertRaises(PlannerError):
            Planner(
                self.settings, ModelRouter(self.settings, provider=provider)
            ).plan("plan")

    def test_unknown_dependency_rejected(self):
        provider = ScriptedProvider(
            [
                plan_json(
                    [
                        {
                            "id": "T1",
                            "title": "a",
                            "dependencies": ["T9"],
                            "checks": [],
                        }
                    ]
                )
            ]
        )
        with self.assertRaises(PlannerError):
            Planner(
                self.settings, ModelRouter(self.settings, provider=provider)
            ).plan("plan")

    def test_unknown_check_type_rejected(self):
        provider = ScriptedProvider(
            [
                plan_json(
                    [
                        {
                            "id": "T1",
                            "title": "a",
                            "checks": [{"type": "vibes", "command": "x"}],
                        }
                    ]
                )
            ]
        )
        with self.assertRaises(PlannerError):
            Planner(
                self.settings, ModelRouter(self.settings, provider=provider)
            ).plan("plan")

    def test_empty_task_list_rejected(self):
        provider = ScriptedProvider([plan_json([])])
        with self.assertRaises(PlannerError):
            Planner(
                self.settings, ModelRouter(self.settings, provider=provider)
            ).plan("plan")


if __name__ == "__main__":
    unittest.main()
