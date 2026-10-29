import contextlib
import io
import json
import subprocess
import unittest

from core.model_router import ModelRouter
from core.orchestrator import Orchestrator
from core.task_runner import TaskRunner
from events.event_bus import EventBus
from state.task_store import TaskStatus
from tests.fixtures.fake_model import (
    ScriptedProvider,
    assistant_text,
    plan_json,
    review_json,
    tool_call,
)
from tests.fixtures.workspace import registry_for, settings_for, temp_project


class OrchestratorIntegrationTests(unittest.TestCase):
    def make(self, root, responses, **overrides):
        settings = settings_for(root, **overrides)
        registry = registry_for(settings)
        provider = ScriptedProvider(responses)
        bus = EventBus(registry.state_root / "state" / "events.jsonl")
        orchestrator = Orchestrator(
            settings,
            registry=registry,
            router=ModelRouter(settings, provider=provider),
            bus=bus,
        )
        return orchestrator, provider, bus

    @staticmethod
    def task_spec(title, path, **extra):
        spec = {
            "id": title,
            "title": title,
            "description": f"Create {path}",
            "dependencies": [],
            "risk": "low",
            "acceptance_criteria": [f"{path} exists"],
            "checks": [{"type": "file_exists", "path": path}],
            "verification": "file exists",
        }
        spec.update(extra)
        return spec

    def test_pipeline_reaches_done_with_evidence(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider, bus = self.make(
                root,
                [
                    tool_call("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                    assistant_text('{"status": "implemented", "summary": "created hello.py"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks(
                [self.task_spec("T1", "hello.py")]
            )

            outcome = orchestrator.execute(tasks, kind="auto")
            task = orchestrator.store.get(tasks[0].id)

            self.assertEqual(outcome, "completed")
            self.assertEqual(task.status, TaskStatus.DONE.value)
            self.assertEqual(task.attempts, 1)
            self.assertEqual(task.verification_result["decision"], "pass")
            self.assertGreaterEqual(task.last_test.get("passed", 0), 1)
            self.assertEqual(provider.remaining, 0)

            evidence = orchestrator.state_root / "evidence" / task.id
            for name in ("build.json", "tests.json", "review.json", "verification.json", "diff.json"):
                self.assertTrue((evidence / name).is_file(), name)

            checkpoint_dir = orchestrator.state_root / "checkpoints" / task.id
            labels = [path.name for path in checkpoint_dir.glob("*.json")]
            self.assertTrue(any("before-build" in name for name in labels))
            self.assertTrue(any("done" in name for name in labels))

            events = [
                json.loads(line)["event"]
                for line in (orchestrator.state_root / "state" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertIn("task.completed", events)
            self.assertIn("verification.passed", events)
            self.assertEqual(orchestrator.runs.latest()["status"], "completed")

    def test_identical_failures_block_and_safe_tasks_continue(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            review = review_json("needs_changes", ["not good enough"], "low")
            orchestrator, provider, _ = self.make(
                root,
                [
                    tool_call("write_file", {"path": "t1.txt", "content": "one"}),
                    assistant_text('{"status": "implemented", "summary": "t1"}'),
                    review,
                    tool_call("write_file", {"path": "t1.txt", "content": "one"}),
                    assistant_text('{"status": "implemented", "summary": "t1"}'),
                    review_json("needs_changes", ["not good enough"], "low"),
                    tool_call("write_file", {"path": "t2.txt", "content": "two"}),
                    assistant_text('{"status": "implemented", "summary": "t2"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks(
                [
                    self.task_spec("T1", "t1.txt"),
                    self.task_spec("T2", "t2.txt"),
                    self.task_spec("T3", "t3.txt", dependencies=["T1"]),
                ]
            )

            outcome = orchestrator.execute(tasks, kind="auto")
            first, second, dependent = [orchestrator.store.get(task.id) for task in tasks]

            self.assertEqual(outcome, "blocked")
            self.assertEqual(first.status, TaskStatus.BLOCKED.value)
            self.assertEqual(first.attempts, 2)
            self.assertEqual(second.status, TaskStatus.DONE.value)
            self.assertEqual(dependent.status, TaskStatus.BLOCKED.value)
            self.assertTrue(
                (
                    orchestrator.state_root
                    / "failures"
                    / f"{first.id}.json"
                ).is_file()
            )
            failure_payload = json.loads(
                (orchestrator.state_root / "failures" / f"{first.id}.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(len(failure_payload["history"]), 2)
            self.assertEqual(failure_payload["last_decision"]["action"], "block")

    def test_commit_happens_only_after_verification_and_scopes_files(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider, _ = self.make(
                root,
                [
                    tool_call("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                    assistant_text('{"status": "implemented", "summary": "created"}'),
                    review_json("approve"),
                ],
                auto_commit=True,
            )
            tasks = orchestrator.store.add_tasks([self.task_spec("T1", "hello.py")])
            orchestrator.execute(tasks, kind="auto")
            task = orchestrator.store.get(tasks[0].id)

            self.assertEqual(task.status, TaskStatus.DONE.value)
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(task.current_commit, head)
            self.assertEqual(task.last_known_good_commit, head)

            log = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("toner:", log)
            self.assertIn(task.id, log)

            committed = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()
            self.assertIn("hello.py", committed)
            self.assertFalse(any(name.startswith(".toner") for name in committed))

    def test_resume_recovers_an_interrupted_task(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider, _ = self.make(
                root,
                [
                    tool_call("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                    assistant_text('{"status": "implemented", "summary": "created"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks([self.task_spec("T1", "hello.py")])
            task = tasks[0]
            task.status = TaskStatus.BUILDING.value
            orchestrator.store.save(task)
            first_run = orchestrator.runs.start([task.id], kind="auto")

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.resume()

            recovered = orchestrator.store.get(task.id)
            self.assertIn("RESUME STATE", output.getvalue())
            self.assertEqual(recovered.status, TaskStatus.DONE.value)
            self.assertEqual(orchestrator.runs.load(first_run["id"])["status"], "abandoned")
            self.assertEqual(orchestrator.runs.latest()["status"], "completed")

    def test_auto_plans_from_markdown_and_executes(self):
        with temp_project(
            files={
                ".toner/files_md/01-plan.md": "# Plan\nCreate hello.py\n",
                "a.py": "print(1)\n",
            },
            git=False,
        ) as root:
            orchestrator, provider, _ = self.make(
                root,
                [
                    plan_json(
                        [
                            {
                                "id": "T1",
                                "title": "Create hello",
                                "description": "create hello.py",
                                "dependencies": [],
                                "risk": "low",
                                "acceptance_criteria": ["hello.py exists"],
                                "checks": [
                                    {"type": "file_exists", "path": "hello.py"}
                                ],
                            }
                        ]
                    ),
                    tool_call("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                    assistant_text('{"status": "implemented", "summary": "created"}'),
                    review_json("approve"),
                ],
            )

            with contextlib.redirect_stdout(io.StringIO()):
                orchestrator.auto()

            tasks = orchestrator.store.all()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].status, TaskStatus.DONE.value)
            self.assertTrue(orchestrator.runs.session().get("plan_fingerprint"))
            self.assertEqual(provider.remaining, 0)

    def test_unproven_claim_is_blocked_not_done(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider, _ = self.make(
                root,
                [
                    tool_call("write_file", {"path": "x.txt", "content": "x"}),
                    assistant_text('{"status": "implemented", "summary": "trust me"}'),
                    review_json("approve"),
                    tool_call("write_file", {"path": "x.txt", "content": "x"}),
                    assistant_text('{"status": "implemented", "summary": "trust me"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks(
                [
                    {
                        "id": "T1",
                        "title": "No checks",
                        "description": "do something",
                        "dependencies": [],
                        "risk": "low",
                        "acceptance_criteria": ["it works"],
                        "checks": [],
                    }
                ]
            )

            outcome = orchestrator.execute(tasks, kind="auto")
            task = orchestrator.store.get(tasks[0].id)

            self.assertEqual(outcome, "blocked")
            self.assertEqual(task.status, TaskStatus.BLOCKED.value)
            self.assertEqual(task.verification_result["decision"], "insufficient")
            self.assertEqual(task.attempts, 2)

    def test_status_and_report_reflect_real_evidence(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, _, _ = self.make(
                root,
                [
                    tool_call("write_file", {"path": "hello.py", "content": "print('hi')\n"}),
                    assistant_text('{"status": "implemented", "summary": "created"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks([self.task_spec("T1", "hello.py")])
            orchestrator.execute(tasks, kind="auto")

            with contextlib.redirect_stdout(io.StringIO()) as status_output:
                orchestrator.status()

            status_text = status_output.getvalue()
            self.assertIn("TONER STATUS", status_text)
            self.assertIn("completed", status_text)
            self.assertIn("TASK-000001", status_text)

            with contextlib.redirect_stdout(io.StringIO()):
                report_path = orchestrator.report()

            report = report_path.read_text(encoding="utf-8")
            self.assertIn("# Toner Engineering Report", report)
            self.assertIn("TASK-000001", report)
            self.assertIn("Verification", report)
            self.assertIn("DONE", report)
            self.assertIn("tests.json", report)


if __name__ == "__main__":
    unittest.main()
