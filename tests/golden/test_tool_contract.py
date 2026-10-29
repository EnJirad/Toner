import subprocess
import unittest
from pathlib import Path

from state.checkpoint import CheckpointManager
from state.failure_manager import FailureManager
from state.task_store import Task, TaskStore
from tests.fixtures.workspace import registry_for, settings_for, temp_project
from tools.git import GitTools


class ToolContractGoldenTests(unittest.TestCase):
    def test_schema_and_dispatch_never_drift(self):
        with temp_project(git=False) as root:
            registry = registry_for(settings_for(root))
            schema_names = {
                entry["function"]["name"] for entry in registry.schemas
            }
            self.assertEqual(schema_names, set(registry.tool_names()))

    def test_destructive_git_operations_are_not_exposed(self):
        for name in ("reset", "clean", "push", "checkout", "rebase", "merge"):
            self.assertFalse(hasattr(GitTools, name), name)

    def test_commit_requires_a_message(self):
        with temp_project(files={"a.py": "x\n"}) as root:
            result = GitTools(root).commit("   ")
            self.assertEqual(result["code"], "commit_message_required")

    def test_commit_stages_only_the_given_files(self):
        with temp_project(files={"a.py": "x\n", "b.py": "y\n"}) as root:
            (root / ".toner").mkdir(exist_ok=True)
            (root / ".toner" / "state.json").write_text("{}", encoding="utf-8")
            git = GitTools(root)

            result = git.commit("toner: golden", files=["a.py"])
            self.assertEqual(result["returncode"], 0, result)

            committed = subprocess.run(
                ["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.split()

            self.assertIn("a.py", committed)
            self.assertNotIn("b.py", committed)
            self.assertNotIn(".toner", committed)

            still_dirty = git.status()["output"]
            self.assertIn(".toner", still_dirty)
            self.assertNotIn("a.py", still_dirty)

    def test_unicode_paths_are_read_and_committed_verbatim(self):
        with temp_project() as root:
            name = "ไฟล์ทดสอบ.txt"
            (root / name).write_text("x\n", encoding="utf-8")
            git = GitTools(root)

            changed = git.changed_files()
            self.assertIn(name, changed["files"])

            result = git.commit("toner: unicode", files=[name])
            self.assertEqual(result["returncode"], 0, result)

            committed = subprocess.run(
                [
                    "git",
                    "-c",
                    "core.quotePath=false",
                    "show",
                    "--name-only",
                    "--pretty=format:",
                    "HEAD",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn(name, committed)

    def test_commit_refuses_unrelated_staged_changes(self):
        with temp_project(files={"a.py": "x\n", "other.py": "y\n"}) as root:
            subprocess.run(
                ["git", "add", "other.py"], cwd=root, check=True
            )
            result = GitTools(root).commit("toner: golden", files=["a.py"])
            self.assertEqual(result["code"], "unrelated_staged_changes")


class SafetyGoldenTests(unittest.TestCase):
    def setUp(self):
        self._context = temp_project(git=False)
        self.root = self._context.__enter__()
        self.registry = registry_for(
            settings_for(self.root, command_timeout=1)
        )

    def tearDown(self):
        self._context.__exit__(None, None, None)

    def test_path_traversal_never_resolves(self):
        result = self.registry.execute("read_file", {"path": "../../etc/passwd"})
        self.assertEqual(result["code"], "path_outside_root")

    def test_secret_files_never_open(self):
        for name in (".env", "id_rsa", "server.pem"):
            result = self.registry.execute("read_file", {"path": name})
            self.assertEqual(result["code"], "secret_file", name)

    def test_dangerous_commands_never_run(self):
        result = self.registry.shell.run("git reset --hard HEAD~1")
        self.assertEqual(result["code"], "dangerous_command")

    def test_commands_time_out_and_are_killed(self):
        result = self.registry.shell.run("sleep 5", timeout=1)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["code"], "command_timeout")

    def test_unknown_tools_are_refused(self):
        result = self.registry.execute("format_disk", {})
        self.assertEqual(result["code"], "unknown_tool")


class PersistenceGoldenTests(unittest.TestCase):
    def test_task_state_survives_a_new_store_instance(self):
        with temp_project(git=False) as root:
            directory = root / ".toner" / "tasks"
            first = TaskStore(directory)
            task = first.add_tasks(
                [
                    {
                        "id": "T1",
                        "title": "Persisted",
                        "checks": [{"type": "file_exists", "path": "x"}],
                    }
                ]
            )[0]
            task.attempts = 2
            first.save(task)

            second = TaskStore(directory)
            reloaded = second.get(task.id)
            self.assertEqual(reloaded.title, "Persisted")
            self.assertEqual(reloaded.attempts, 2)
            self.assertEqual(reloaded.checks[0]["path"], "x")

    def test_checkpoint_survives_and_records_sha(self):
        with temp_project() as root:
            git = GitTools(root)
            sha = git.current_sha()["sha"]
            manager = CheckpointManager(root / ".toner" / "checkpoints", git)
            task = Task(id="TASK-000001", title="x")
            manager.create("before-build", task, "RUN-000001")

            from state.checkpoint import CheckpointManager as Reloaded

            payload = Reloaded(
                root / ".toner" / "checkpoints", git
            ).latest("TASK-000001")
            self.assertEqual(payload["git"]["sha"], sha)
            self.assertEqual(payload["task_id"], "TASK-000001")

    def test_identical_failure_stops_the_loop(self):
        with temp_project(git=False) as root:
            manager = FailureManager(
                root / ".toner" / "failures",
                max_attempts=5,
                max_identical=2,
                backoff_seconds=0,
            )
            task = Task(id="TASK-000001")
            task.attempts = 1
            first = manager.record(task, manager.from_text("AssertionError: same"))
            task.attempts = 2
            second = manager.record(task, manager.from_text("AssertionError: same"))

            self.assertEqual(first.action, "retry")
            self.assertEqual(second.action, "block")
            self.assertTrue((root / ".toner/failures/TASK-000001.json").is_file())


if __name__ == "__main__":
    unittest.main()
