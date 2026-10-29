import unittest

from core.model_router import (
    ModelError,
    ModelRouter,
    OpenAICompatibleProvider,
)
from tests.fixtures.fake_model import ScriptedProvider, assistant_text
from tests.fixtures.workspace import settings_for, temp_dir


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def ok_payload(content="hello"):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


class ProviderTests(unittest.TestCase):
    def provider(self, response, api_key="key"):
        return OpenAICompatibleProvider(
            "http://api.invalid/v1/chat/completions",
            api_key,
            "model-x",
            timeout=5,
            post=lambda *args, **kwargs: response,
        )

    def test_successful_message(self):
        provider = self.provider(FakeResponse(200, ok_payload("hi")))
        message = provider.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(message["content"], "hi")

    def test_missing_api_key_is_permanent(self):
        provider = self.provider(FakeResponse(200, ok_payload()), api_key="")
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(ctx.exception.transient)

    def test_rate_limit_is_transient(self):
        provider = self.provider(FakeResponse(429, {}))
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.transient)
        self.assertEqual(ctx.exception.status, 429)

    def test_bad_request_is_permanent(self):
        provider = self.provider(FakeResponse(400, {}))
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(ctx.exception.transient)

    def test_non_json_is_transient(self):
        provider = self.provider(FakeResponse(200, ValueError("nope")))
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.transient)

    def test_missing_url_or_model_is_permanent(self):
        provider = OpenAICompatibleProvider("", "key", "", post=lambda *a, **k: None)
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(ctx.exception.transient)

    def test_missing_choices_is_transient(self):
        provider = self.provider(FakeResponse(200, {}))
        with self.assertRaises(ModelError) as ctx:
            provider.chat([{"role": "user", "content": "hi"}])
        self.assertTrue(ctx.exception.transient)


class RouterTests(unittest.TestCase):
    def test_role_model_override(self):
        with temp_dir() as root:
            settings = settings_for(root, models={"reviewer": "review-model"})
            provider = ScriptedProvider(
                [assistant_text("ok"), assistant_text("ok")]
            )
            router = ModelRouter(settings, provider=provider)
            router.chat("reviewer", [{"role": "user", "content": "x"}])
            self.assertEqual(provider.calls[0]["model"], "review-model")

            router.chat("builder", [{"role": "user", "content": "x"}])
            self.assertEqual(provider.calls[1]["model"], settings["model"])

    def test_unsupported_provider_type_rejected(self):
        with temp_dir() as root:
            settings = settings_for(root, provider={"type": "gemini"})
            with self.assertRaises(ValueError):
                ModelRouter(settings)


if __name__ == "__main__":
    unittest.main()
