"""End-to-end conversation tests: chat, inspection, tests, follow-ups,
cancellation, API errors and task attribution — driven through
``Orchestrator.handle`` exactly as the CLI does."""

import contextlib
import io
import json
import unittest

from core.interrupts import request_stop, reset_stop
from core.model_router import ModelError, ModelRouter
from core.orchestrator import Orchestrator
from events.event_bus import EventBus
from policy.security import SecretRedactor
from state.task_store import TaskStatus
from tests.fixtures.fake_model import (
    ScriptedProvider,
    assistant_text,
    review_json,
    tool_call,
)
from tests.fixtures.workspace import registry_for, settings_for, temp_project


class ConversationTests(unittest.TestCase):
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
        return orchestrator, provider

    @staticmethod
    def task_spec(title, path):
        return {
            "id": title,
            "title": title,
            "description": f"Create {path}",
            "dependencies": [],
            "risk": "low",
            "acceptance_criteria": [f"{path} exists"],
            "checks": [{"type": "file_exists", "path": path}],
            "verification": "file exists",
        }

    @staticmethod
    def events(orchestrator):
        path = orchestrator.state_root / "state" / "events.jsonl"

        if not path.is_file():
            return []

        return [
            json.loads(line).get("event")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def event_payloads(orchestrator):
        path = orchestrator.state_root / "state" / "events.jsonl"

        if not path.is_file():
            return []

        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def project_files(root):
        return sorted(
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and ".toner" not in path.parts
        )

    # -- 16: chat must not execute project tools -------------------------

    def test_greeting_is_chat_and_runs_no_project_tools(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [assistant_text("Hi! I'm Toner. What would you like to work on?")],
            )
            before = self.project_files(root)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("hi")

            text = output.getvalue()
            self.assertIn("What would you like to work on", text)
            self.assertEqual(provider.remaining, 0)
            # The chat call must not advertise any tools.
            self.assertIsNone(provider.calls[0]["tools"])
            self.assertEqual(self.project_files(root), before)
            evidence_dir = orchestrator.state_root / "evidence"
            self.assertFalse(
                any(evidence_dir.iterdir()),
                "chat must not create task evidence",
            )
            self.assertIn("intent.classified", self.events(orchestrator))
            self.assertEqual(orchestrator.conversation.last_intent, "CHAT")

    def test_thai_greeting_is_chat(self):
        with temp_project() as root:
            orchestrator, provider = self.make(
                root, [assistant_text("สวัสดีครับ มีอะไรให้ช่วยบ้าง?")]
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("สวัสดี")

            self.assertIn("สวัสดีครับ", output.getvalue())
            self.assertIsNone(provider.calls[0]["tools"])

    def test_conversation_history_is_kept_across_turns(self):
        with temp_project() as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text("Hello again!"),
                    assistant_text("I can chat, inspect and code."),
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()):
                orchestrator.handle("hi")
                orchestrator.handle("โอเค")  # pleasantry → chat fast path

            self.assertEqual(provider.remaining, 0)
            second_call = provider.calls[1]["messages"]
            contents = [item.get("content") for item in second_call]
            self.assertIn("hi", contents)
            self.assertIn("Hello again!", contents)

    # -- 1/2/3/7: routing through handle() --------------------------------

    def test_question_uses_read_only_inspection_not_coding(self):
        with temp_project(files={"app.py": "raise SyntaxError\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "QUESTION", "confidence": 0.92, '
                        '"reason": "user asks why"}'
                    ),
                    tool_call("read_file", {"path": "app.py"}),
                    assistant_text("app.py contains a deliberate SyntaxError."),
                ],
            )
            before = self.project_files(root)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("why does the build fail?")

            text = output.getvalue()
            self.assertIn("SyntaxError", text)
            self.assertEqual(self.project_files(root), before)
            classified = [
                item
                for item in self.event_payloads(orchestrator)
                if item.get("event") == "intent.classified"
            ]
            self.assertEqual(len(classified), 1)
            self.assertEqual(classified[0]["data"].get("intent"), "QUESTION")

    def test_inspection_never_modifies_files(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "INSPECT", "confidence": 0.95, '
                        '"reason": "inspect request"}'
                    ),
                    tool_call("write_file", {"path": "hack.txt", "content": "bad"}),
                    assistant_text("I inspected the project."),
                ],
            )
            before = self.project_files(root)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("ตรวจสอบโปรเจกต์ให้หน่อย")

            text = output.getvalue()
            self.assertFalse((root / "hack.txt").exists())
            self.assertEqual(self.project_files(root), before)
            self.assertIn("refused", text)
            self.assertIn("I inspected the project", text)

    def test_ambiguous_message_asks_a_question_without_tools(self):
        with temp_project() as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "CLARIFICATION", "confidence": 0.4, '
                        '"clarification_question": "Should I inspect the '
                        'project or start a coding task?"}'
                    )
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("qwertyuiop")

            text = output.getvalue()
            self.assertIn("Should I inspect the project", text)
            # Only the classifier call happened; no tool-bearing calls.
            self.assertEqual(len(provider.calls), 1)

    def test_unclassifiable_model_falls_back_to_a_safe_question(self):
        with temp_project() as root:
            orchestrator, provider = self.make(
                root, [ModelError("rate limited", transient=True)]
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("zyxwvu")

            text = output.getvalue()
            self.assertIn("not sure what you'd like me to do", text)

    # -- 5: test requests -------------------------------------------------

    def test_test_request_runs_the_verification_workflow(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "TEST", "confidence": 0.99, '
                        '"reason": "run a check"}'
                    )
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("run python3 -m py_compile app.py")

            text = output.getvalue()
            self.assertIn("[passed]", text)
            self.assertIn("executed", text)
            self.assertEqual(provider.remaining, 0)
            self.assertTrue(
                (
                    orchestrator.state_root
                    / "evidence"
                    / "TEST-ADHOC"
                    / "tests.json"
                ).is_file()
            )

    def test_failing_check_is_reported_as_failed(self):
        with temp_project(files={"app.py": "def broken(:\n"}) as root:
            orchestrator, _ = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "TEST", "confidence": 0.99, "reason": "x"}'
                    )
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("run python3 -m py_compile app.py")

            text = output.getvalue()
            self.assertIn("[failed]", text)
            self.assertIn("failed 1", text)

    def test_test_request_without_configuration_explains_itself(self):
        with temp_project() as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "TEST", "confidence": 0.9, "reason": "x"}'
                    )
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("run the test suite")

            self.assertIn("No verification is configured", output.getvalue())
            self.assertEqual(provider.remaining, 0)

    # -- 6: follow-ups -----------------------------------------------------

    def test_follow_up_continues_the_active_task(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    tool_call(
                        "write_file", {"path": "hello.py", "content": "print('hi')\n"}
                    ),
                    assistant_text(
                        '{"status": "implemented", "summary": "created hello.py"}'
                    ),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks(
                [self.task_spec("T1", "hello.py")]
            )

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("ทำต่อ")

            self.assertEqual(provider.remaining, 0)
            self.assertEqual(
                orchestrator.store.get(tasks[0].id).status, TaskStatus.DONE.value
            )
            self.assertIn("Continuing the active work", output.getvalue())

    def test_follow_up_without_a_task_explains_and_does_nothing(self):
        with temp_project() as root:
            orchestrator, provider = self.make(root, [])
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("ทำต่อ")

            text = output.getvalue()
            self.assertIn("There is no active task", text)
            self.assertNotIn("could not reach the model", text)
            self.assertEqual(provider.remaining, 0)

    def test_test_follow_up_runs_tests(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "TEST", "confidence": 0.9, "reason": "x"}'
                    )
                ],
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("รัน python3 -m py_compile app.py")

            self.assertIn("[passed]", output.getvalue())

    # -- 12: cancellation ---------------------------------------------------

    def test_cancellation_stops_further_execution(self):
        with temp_project(files={"a.py": "print(1)\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    tool_call(
                        "write_file", {"path": "hello.py", "content": "x"}
                    ),
                    assistant_text('{"status": "implemented", "summary": "x"}'),
                    review_json("approve"),
                ],
            )
            tasks = orchestrator.store.add_tasks(
                [self.task_spec("T1", "hello.py")]
            )
            request_stop()

            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    orchestrator.handle("ทำต่อ")
            finally:
                reset_stop()

            task = orchestrator.store.get(tasks[0].id)
            self.assertNotEqual(task.status, TaskStatus.DONE.value)
            self.assertEqual(provider.remaining, 3, "no model call after cancel")

    # -- 13: API errors -----------------------------------------------------

    def test_chat_api_error_is_reported_not_crashed(self):
        with temp_project() as root:
            orchestrator, _ = self.make(
                root, [ModelError("HTTP 503", transient=True)]
            )
            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle("hi")

            self.assertIn("could not reach the model", output.getvalue())
            self.assertIn("HTTP 503", output.getvalue())

    # -- 17: completed coding task reports diff + verification ---------------

    def test_coding_task_reports_diff_and_verification(self):
        with temp_project(files={"app.py": "x = 1\n"}) as root:
            orchestrator, provider = self.make(
                root,
                [
                    assistant_text(
                        '{"intent": "CODE_TASK", "confidence": 0.97, '
                        '"reason": "explicit fix request"}'
                    ),
                    tool_call(
                        "write_file",
                        {"path": "hello.py", "content": "print('hi')\n"},
                    ),
                    assistant_text(
                        '{"status": "implemented", "summary": "created hello.py"}'
                    ),
                    review_json("approve"),
                ],
            )
            # A user file that exists before the task must not be claimed.
            (root / "user_notes.txt").write_text("mine\n", encoding="utf-8")
            before = self.project_files(root)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                orchestrator.handle(
                    "create a file called hello.py that prints hi"
                )

            text = output.getvalue()
            self.assertEqual(provider.remaining, 0)
            self.assertIn("Starting TASK-", text)
            self.assertIn("DONE", text)
            self.assertIn("Changed by this task", text)
            self.assertIn("hello.py", text)
            self.assertIn("Checks — executed:", text)

            # The pre-existing untracked file is reported as pre-existing.
            self.assertIn("Untracked files that existed before this task", text)
            changed_section = text.split("Changed by this task:")[1].split(
                "Pre-existing"
            )[0]
            self.assertIn("hello.py", changed_section)
            self.assertNotIn("user_notes.txt", changed_section)

            task = orchestrator.store.all()[0]
            self.assertEqual(task.status, TaskStatus.DONE.value)
            self.assertGreaterEqual(task.last_test.get("executed", 0), 1)

            evidence_dir = orchestrator.state_root / "evidence" / task.id
            for name in (
                "build.json",
                "diff.json",
                "review.json",
                "tests.json",
                "verification.json",
                "changes.json",
            ):
                self.assertTrue((evidence_dir / name).is_file(), name)

            changes = json.loads(
                (evidence_dir / "changes.json").read_text(encoding="utf-8")
            )["data"]
            self.assertIn("hello.py", changes["changed_by_task"])
            self.assertNotIn("user_notes.txt", changes["changed_by_task"])
            self.assertIn("user_notes.txt", changes["pre_existing_untracked"])

            # The task touched exactly one new file; app.py was not modified.
            self.assertEqual(
                sorted(set(self.project_files(root)) - set(before)), ["hello.py"]
            )

    # -- 14: secrets are redacted in conversation state ---------------------

    def test_conversation_history_redacts_secrets(self):
        from core.conversation import Conversation

        conversation = Conversation(redactor=SecretRedactor())
        conversation.add(
            "assistant", "connecting with sk-abcdefghijklmnop to the API"
        )
        self.assertNotIn("sk-abcdefghijklmnop", conversation.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
