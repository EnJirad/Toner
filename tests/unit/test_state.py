import json
import tempfile
import unittest
from pathlib import Path

from core.context_manager import ContextManager
from core.model_router import ModelRouter
from policy.security import SecretRedactor
from state.checkpoint import CheckpointManager
from state.evidence import EvidenceStore
from state.failure_manager import FailureManager, FailureType
from state.run_store import RunStore
from state.task_store import (
    InvalidTransition,
    Task,
    TaskStatus,
    TaskStore,
)
from tests.fixtures.fake_model import ScriptedProvider, assistant_text
from tests.fixtures.workspace import temp_dir, temp_project
from tools.git import GitTools


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name) / "tasks"
        self.store = TaskStore(self.directory)

    def tearDown(self):
        self._tmp.cleanup()

    def test_add_tasks_assigns_canonical_ids_and_remaps_dependencies(self):
        tasks = self.store.add_tasks(
            [
                {
                    "id": "T1",
                    "title": "First",
                    "checks": [{"type": "file_exists", "path": "a"}],
                },
                {
                    "id": "T2",
                    "title": "Second",
                    "dependencies": ["T1"],
                },
            ]
        )
        self.assertEqual([task.id for task in tasks], ["TASK-000001", "TASK-000002"])
        self.assertEqual(tasks[1].dependencies, ["TASK-000001"])

    def test_next_task_respects_dependencies(self):
        first, second = self.store.add_tasks(
            [
                {"id": "T1", "title": "First"},
                {"id": "T2", "title": "Second", "dependencies": ["T1"]},
            ]
        )
        self.assertEqual(self.store.next_task().id, first.id)

        first.status = TaskStatus.DONE.value
        self.store.save(first)
        self.assertEqual(self.store.next_task().id, second.id)

    def test_invalid_transition_rejected(self):
        task = Task(id="TASK-000001", status=TaskStatus.DONE.value)
        with self.assertRaises(InvalidTransition):
            task.transition(TaskStatus.BUILDING)

    def test_unknown_status_rejected(self):
        with self.assertRaises(InvalidTransition):
            Task.from_dict({"id": "TASK-000001", "status": "banana"})

    def test_roundtrip_persistence(self):
        task = Task(id="TASK-000001", title="Persist", attempts=2)
        self.store.save(task)
        loaded = self.store.get("TASK-000001")
        self.assertEqual(loaded.title, "Persist")
        self.assertEqual(loaded.attempts, 2)

    def test_corrupt_task_file_raises(self):
        (self.directory / "TASK-000009.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            self.store.all()

    def test_legacy_import(self):
        state_file = self.directory.parent / "auto_state.json"
        state_file.write_text(
            json.dumps(
                {
                    "project_root": "/tmp/x",
                    "tasks": [
                        {
                            "id": "T001",
                            "title": "Legacy done",
                            "status": "completed",
                            "attempts": 1,
                            "dependencies": [],
                            "acceptance_criteria": ["works"],
                        },
                        {
                            "id": "T002",
                            "title": "Legacy pending",
                            "status": "in_progress",
                            "dependencies": ["T001"],
                            "acceptance_criteria": [],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        count = self.store.import_legacy(state_file)
        self.assertEqual(count, 2)
        imported = self.store.all()
        self.assertEqual(imported[0].status, TaskStatus.DONE.value)
        self.assertEqual(imported[1].status, TaskStatus.QUEUED.value)
        self.assertEqual(imported[1].dependencies, ["TASK-000001"])
        self.assertFalse(imported[1].require_checks)
        self.assertEqual(self.store.import_legacy(state_file), 0)

    def test_counts(self):
        tasks = self.store.add_tasks([{"id": "T1"}, {"id": "T2"}])
        tasks[0].status = TaskStatus.DONE.value
        self.store.save(tasks[0])
        counts = self.store.counts()
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["queued"], 1)


class RunStoreTests(unittest.TestCase):
    def test_start_finish_and_session(self):
        with temp_dir() as root:
            store = RunStore(root / "runs", root / "state")
            run = store.start(["TASK-000001"], kind="auto", session_limit=5)
            self.assertEqual(run["id"], "RUN-000001")
            self.assertIsNotNone(store.active())
            self.assertEqual(store.session()["status"], "running")

            store.finish(run, "completed")
            self.assertIsNone(store.active())
            self.assertEqual(store.session()["status"], "completed")
            self.assertEqual(store.latest()["id"], "RUN-000001")
            self.assertEqual(store.load("RUN-000001")["status"], "completed")

    def test_check_run_does_not_clobber_plan_session(self):
        with temp_dir() as root:
            store = RunStore(root / "runs", root / "state")
            store.update_session(
                status="paused", plan_fingerprint="abc", run_id="RUN-000001"
            )

            run = store.start([], kind="check", update_session=False)
            store.finish(run, "completed", update_session=False)

            session = store.session()
            self.assertEqual(session["status"], "paused")
            self.assertEqual(session["plan_fingerprint"], "abc")
            self.assertEqual(session["run_id"], "RUN-000001")


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_records_git_sha_and_history(self):
        with temp_project() as root:
            git = GitTools(root)
            git_sha = git.current_sha()["sha"]
            manager = CheckpointManager(root / ".toner" / "checkpoints", git)
            task = Task(id="TASK-000001", title="x")

            payload = manager.create("before-build", task, "RUN-000001")

            self.assertEqual(payload["git"]["sha"], git_sha)
            self.assertEqual(payload["task_id"], "TASK-000001")
            self.assertTrue(
                (root / ".toner/checkpoints/TASK-000001/0001-before-build.json").is_file()
            )
            self.assertTrue((root / ".toner/checkpoints/TASK-000001.json").is_file())
            self.assertEqual(manager.latest("TASK-000001")["label"], "before-build")


class EvidenceTests(unittest.TestCase):
    def test_evidence_redacts_secrets(self):
        with temp_project(git=False) as root:
            store = EvidenceStore(
                root,
                root / ".toner" / "evidence",
                SecretRedactor(["supersecretvalue"]),
            )
            relative = store.write(
                "TASK-000001",
                "tests",
                {
                    "api_key": "abcdefghijk",
                    "nested": {"password": "hunter2hunter2"},
                    "text": "Bearer abcdefghijklmnop supersecretvalue",
                },
            )
            self.assertTrue(relative.endswith("tests.json"))

            data = store.read("TASK-000001", "tests")["data"]
            self.assertEqual(data["api_key"], "[REDACTED]")
            self.assertEqual(data["nested"]["password"], "[REDACTED]")
            self.assertIn("[REDACTED]", data["text"])
            self.assertNotIn("supersecretvalue", json.dumps(data))
            self.assertEqual(store.names("TASK-000001"), ["tests"])

    def test_unsafe_task_id_rejected(self):
        with temp_project(git=False) as root:
            store = EvidenceStore(root, root / ".toner" / "evidence")
            with self.assertRaises(ValueError):
                store.write("../../escape", "x", {})


class FailureManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)
        self.manager = FailureManager(
            self.directory,
            max_attempts=3,
            max_identical=2,
            backoff_seconds=0,
            backoff_max_seconds=0,
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_classification(self):
        self.assertEqual(
            self.manager.classify("connection reset by peer"),
            FailureType.TRANSIENT,
        )
        self.assertEqual(
            self.manager.classify("HTTP 429 Too Many Requests"),
            FailureType.TRANSIENT,
        )
        self.assertEqual(
            self.manager.classify("ModuleNotFoundError: No module named 'x'"),
            FailureType.PERMANENT,
        )
        self.assertEqual(
            self.manager.classify("HTTP 401 Unauthorized"),
            FailureType.PERMANENT,
        )
        self.assertEqual(
            self.manager.classify("dangerous_command blocked"),
            FailureType.DANGEROUS,
        )
        self.assertEqual(
            self.manager.classify("AssertionError: expected 1 got 2"),
            FailureType.RECOVERABLE,
        )

    def test_fingerprint_stability_and_change(self):
        one = self.manager.from_text(
            'File "auth.py", line 142: TypeError: missing_token'
        )
        two = self.manager.from_text(
            'File "auth.py", line 142: TypeError: missing_token'
        )
        three = self.manager.from_text(
            'File "auth.py", line 143: TypeError: missing_token'
        )
        self.assertEqual(one.signature, two.signature)
        self.assertNotEqual(one.signature, three.signature)
        self.assertEqual(one.file, "auth.py")
        self.assertEqual(one.line, 142)
        self.assertEqual(one.kind, "TypeError")

    def test_identical_failure_blocks(self):
        task = Task(id="TASK-000001")
        message = "AssertionError: still broken"

        task.attempts = 1
        first = self.manager.record(task, self.manager.from_text(message))
        self.assertEqual(first.action, "retry")

        task.attempts = 2
        second = self.manager.record(task, self.manager.from_text(message))
        self.assertEqual(second.action, "block")
        self.assertIn("Identical failure", second.reason)

    def test_changed_failure_can_retry(self):
        task = Task(id="TASK-000001")

        task.attempts = 1
        first = self.manager.record(task, self.manager.from_text("Error A"))
        task.attempts = 2
        second = self.manager.record(task, self.manager.from_text("Error B"))

        self.assertEqual(first.action, "retry")
        self.assertEqual(second.action, "retry")

    def test_attempt_budget_blocks(self):
        task = Task(id="TASK-000001")
        task.attempts = 3
        decision = self.manager.record(
            task, self.manager.from_text("AssertionError: unique")
        )
        self.assertEqual(decision.action, "block")
        self.assertIn("Attempt budget", decision.reason)

    def test_dangerous_failure_blocks_immediately(self):
        task = Task(id="TASK-000001")
        task.attempts = 1
        decision = self.manager.record(
            task, self.manager.from_text("dangerous_command: refused")
        )
        self.assertEqual(decision.action, "block")
        self.assertEqual(decision.classification, FailureType.DANGEROUS.value)

    def test_history_is_persisted(self):
        task = Task(id="TASK-000001")
        task.attempts = 1
        self.manager.record(task, self.manager.from_text("Error A"))
        payload = json.loads(
            (self.directory / "TASK-000001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(payload["history"]), 1)
        self.assertEqual(task.failure_count, 1)


class ContextManagerTests(unittest.TestCase):
    def _messages(self):
        messages = [
            {"role": "system", "content": "RULES " * 20},
            {"role": "user", "content": "TASK " * 20},
        ]
        for _ in range(20):
            messages.append({"role": "assistant", "content": "A" * 300})
            messages.append({"role": "tool", "content": "T" * 300})
        return messages

    def test_compaction_preserves_rules_and_task(self):
        messages = self._messages()
        manager = ContextManager(max_chars=2000)
        compacted, info = manager.compact(messages)

        self.assertTrue(info["compacted"])
        self.assertEqual(compacted[0]["role"], "system")
        self.assertIn("RULES", compacted[0]["content"])
        self.assertTrue(
            any(
                message["role"] == "user" and "TASK" in message["content"]
                for message in compacted
            )
        )
        self.assertLess(manager.size(compacted), manager.size(messages))
        self.assertTrue(
            any(
                "Context compacted" in str(message.get("content"))
                for message in compacted
            )
        )

    def test_summary_path_used_when_router_available(self):
        provider = ScriptedProvider([assistant_text("summary of earlier work")])
        manager = ContextManager(
            max_chars=2000,
            router=ModelRouter({"model": "test"}, provider=provider),
        )
        compacted, info = manager.compact(self._messages())

        self.assertTrue(info["compacted"])
        self.assertEqual(info["method"], "summary")
        self.assertTrue(
            any(
                "summary of earlier work" in str(message.get("content"))
                for message in compacted
            )
        )

    def test_small_conversation_untouched(self):
        messages = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        manager = ContextManager(max_chars=2000)
        compacted, info = manager.compact(messages)
        self.assertFalse(info["compacted"])
        self.assertEqual(compacted, messages)


if __name__ == "__main__":
    unittest.main()
