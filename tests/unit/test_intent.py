"""Intent router tests: the scenarios that decide what Toner does with a line."""

import unittest

from core.config import default_settings
from core.intent import Intent, IntentRouter
from core.model_router import ModelError, ModelRouter
from tests.fixtures.fake_model import ScriptedProvider, assistant_text


def router_without_model():
    return IntentRouter(settings=None, model_router=None)


def model_backed(responses):
    settings = default_settings()
    settings.update(
        {
            "api_key": "test-key-not-real",
            "api_url": "http://api.invalid/v1/chat/completions",
            "model": "test-model",
        }
    )
    provider = ScriptedProvider(responses)
    return (
        IntentRouter(
            settings,
            model_router=ModelRouter(settings, provider=provider),
        ),
        provider,
    )


class GreetingTests(unittest.TestCase):
    def test_english_and_thai_greetings_route_to_chat(self):
        router = router_without_model()

        for message in (
            "hi",
            "Hello!",
            "hey",
            "สวัสดี",
            "สวัสดีครับ",
            "เป็นไงบ้าง",
            "หวัดดี",
            "thanks",
            "โอเค",
        ):
            result = router.classify(message)
            self.assertEqual(
                result.intent, Intent.CHAT.value, f"{message!r} -> {result.intent}"
            )

    def test_greeting_with_real_request_is_not_chat(self):
        router = router_without_model()
        result = router.classify("hello, fix the login bug please")
        self.assertNotEqual(result.intent, Intent.CHAT.value)


class QuestionTests(unittest.TestCase):
    def test_general_questions_are_not_code_tasks(self):
        router = router_without_model()

        for message in (
            "why is the build failing",
            "ทำไม build ไม่ผ่าน",
            "what does this function do",
            "how does the auth flow work?",
            "อธิบายว่าโปรเจกต์นี้ทำงานยังไง",
        ):
            result = router.classify(message)
            self.assertEqual(
                result.intent,
                Intent.QUESTION.value,
                f"{message!r} -> {result.intent}",
            )

    def test_explain_request_is_a_question(self):
        router = router_without_model()
        result = router.classify("ช่วยอธิบายว่าโมดูลนี้ทำอะไร")
        self.assertEqual(result.intent, Intent.QUESTION.value)


class CodeTaskTests(unittest.TestCase):
    def test_code_edit_requests_route_to_code_task(self):
        router = router_without_model()

        for message in (
            "fix the login bug",
            "ช่วยแก้บั๊กนี้",
            "implement a new endpoint for users",
            "refactor the database layer",
            "เพิ่มฟีเจอร์ export เป็น CSV",
        ):
            result = router.classify(message)
            self.assertEqual(
                result.intent,
                Intent.CODE_TASK.value,
                f"{message!r} -> {result.intent}",
            )


class InspectAndPlanTests(unittest.TestCase):
    def test_inspection_requests_route_to_inspect(self):
        router = router_without_model()

        for message in (
            "ตรวจสอบโปรเจกต์ให้หน่อย",
            "inspect this project and tell me what needs fixing",
            "diagnose why the app crashes",
        ):
            result = router.classify(message)
            self.assertEqual(
                result.intent,
                Intent.INSPECT.value,
                f"{message!r} -> {result.intent}",
            )

    def test_plan_requests_route_to_plan(self):
        router = router_without_model()
        result = router.classify("วางแผนการย้ายระบบ auth ให้หน่อย")
        self.assertEqual(result.intent, Intent.PLAN.value)

    def test_test_requests_route_to_test(self):
        router = router_without_model()

        for message in ("run the tests", "รัน pytest ให้หน่อย"):
            result = router.classify(message)
            self.assertEqual(
                result.intent, Intent.TEST.value, f"{message!r} -> {result.intent}"
            )

    def test_git_requests_route_to_git_task(self):
        router = router_without_model()
        result = router.classify("show me git status")
        self.assertEqual(result.intent, Intent.GIT_TASK.value)


class FollowUpTests(unittest.TestCase):
    def test_continue_reuses_active_task_context(self):
        router = router_without_model()
        result = router.classify("ทำต่อ", active_task="TASK-000001 [BUILDING] fix bug")

        self.assertEqual(result.intent, Intent.CODE_TASK.value)
        self.assertTrue(result.follow_up)
        self.assertEqual(result.source, "follow_up")

    def test_continue_without_task_still_follow_up(self):
        router = router_without_model()
        result = router.classify("continue")
        self.assertEqual(result.intent, Intent.CODE_TASK.value)
        self.assertTrue(result.follow_up)

    def test_repeat_test_request_is_test_intent(self):
        router = router_without_model()
        result = router.classify("ทดสอบอีกครั้ง")
        self.assertEqual(result.intent, Intent.TEST.value)
        self.assertTrue(result.follow_up)


class ClarificationTests(unittest.TestCase):
    def test_ambiguous_message_asks_instead_of_coding(self):
        router = router_without_model()

        for message in ("xyzzy plugh", "!!! ??? ...", "12345"):
            result = router.classify(message)
            self.assertEqual(
                result.intent,
                Intent.CLARIFICATION.value,
                f"{message!r} -> {result.intent}",
            )
            self.assertTrue(result.clarification_question)

    def test_empty_message_is_clarification(self):
        result = router_without_model().classify("   ")
        self.assertEqual(result.intent, Intent.CLARIFICATION.value)


class ModelClassificationTests(unittest.TestCase):
    def test_model_classification_is_used_when_available(self):
        router, provider = model_backed(
            [
                assistant_text(
                    '{"intent": "INSPECT", "confidence": 0.93, '
                    '"reason": "user asks for a review"}'
                )
            ]
        )
        result = router.classify("can you take a look at things")

        self.assertEqual(result.intent, Intent.INSPECT.value)
        self.assertEqual(result.source, "model")
        self.assertEqual(provider.remaining, 0)

    def test_model_error_falls_back_to_heuristics(self):
        router, provider = model_backed(
            [ModelError("boom", transient=True)]
        )
        result = router.classify("fix the login bug")

        self.assertEqual(result.intent, Intent.CODE_TASK.value)
        self.assertEqual(result.source, "heuristic")

    def test_invalid_model_answer_falls_back_to_heuristics(self):
        router, _ = model_backed(
            [assistant_text('{"intent": "NOT_A_REAL_INTENT"}')]
        )
        result = router.classify("fix the login bug")
        self.assertEqual(result.intent, Intent.CODE_TASK.value)
        self.assertEqual(result.source, "heuristic")

    def test_model_reply_without_json_falls_back(self):
        router, _ = model_backed([assistant_text("I think you should talk about it")])
        result = router.classify("???")
        self.assertEqual(result.intent, Intent.CLARIFICATION.value)

    def test_greeting_short_circuits_before_the_model(self):
        router, provider = model_backed([assistant_text("unreachable")])
        result = router.classify("สวัสดี")
        self.assertEqual(result.intent, Intent.CHAT.value)
        self.assertEqual(result.source, "fast_path")
        self.assertEqual(provider.remaining, 1)


if __name__ == "__main__":
    unittest.main()
