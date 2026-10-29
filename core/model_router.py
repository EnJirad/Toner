"""Model abstraction and role-based routing.

The orchestration layer never talks to HTTP directly. A provider implements
``chat``; the router decides which provider/model serves each role. Only the
OpenAI-compatible provider is implemented today, and one model can serve every
role, so no additional paid API is required.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class ModelError(RuntimeError):
    """A model call failure with enough information to classify a retry."""

    def __init__(self, message, transient=False, status=None):
        super().__init__(message)
        self.transient = bool(transient)
        self.status = status


class ModelProvider(ABC):
    name = "base"

    @abstractmethod
    def chat(self, messages, tools=None, temperature=0.1, model=None):
        """Return an assistant message dict matching the Chat Completions shape."""
        raise NotImplementedError


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"

    def __init__(self, api_url, api_key, model, timeout=120, post=None):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = int(timeout)
        self._post = post

    def _transport(self):
        if self._post is not None:
            return self._post

        try:
            import requests
        except ImportError as exc:
            raise ModelError(
                "The 'requests' package is required for API calls: "
                "python -m pip install requests",
                transient=False,
            ) from exc

        return requests.post

    def chat(self, messages, tools=None, temperature=0.1, model=None):
        if not self.api_key:
            raise ModelError(
                "No API key configured. Set AIUNLIMIT_API_KEY or api_key in "
                "settings.json.",
                transient=False,
            )

        if not self.api_url or not (model or self.model):
            raise ModelError(
                "api_url and model must be configured before calling a model",
                transient=False,
            )

        payload = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        try:
            response = self._transport()(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.timeout,
            )
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError(str(exc), transient=True) from exc

        status = getattr(response, "status_code", None)

        if not getattr(response, "ok", False):
            # The response body is intentionally not surfaced: it may echo
            # request content or credentials.
            transient = status in (408, 409, 425, 429) or (
                isinstance(status, int) and 500 <= status <= 599
            )
            raise ModelError(
                f"API HTTP {status}; check URL, API key, model and request format",
                transient=transient,
                status=status,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ModelError("API returned a non-JSON response", transient=True) from exc

        choices = data.get("choices")

        if not isinstance(choices, list) or not choices:
            raise ModelError("API response did not include 'choices'", transient=True)

        message = choices[0].get("message")

        if not isinstance(message, dict):
            raise ModelError("API response did not include a valid message", transient=True)

        return message


class ModelRouter:
    """Resolve a provider and model per role."""

    ROLES = (
        "planner",
        "builder",
        "reviewer",
        "qa",
        "verifier",
        "chat",
        "router",
        "inspector",
    )

    def __init__(self, settings, provider=None):
        self.settings = settings
        self._provider = provider or self._build_provider()

    def _build_provider(self):
        config = dict(self.settings.get("provider") or {})
        provider_type = config.get("type", "openai_compatible")

        if provider_type != "openai_compatible":
            raise ValueError(
                f"Unsupported provider type {provider_type!r}; "
                "add a ModelProvider adapter for it"
            )

        return OpenAICompatibleProvider(
            api_url=config.get("api_url") or self.settings.get("api_url", ""),
            api_key=self.settings.get("api_key", ""),
            model=config.get("model") or self.settings.get("model", ""),
            timeout=self.settings.get("request_timeout", 120),
        )

    def provider_for(self, role="builder"):
        return self._provider

    def model_for(self, role="builder"):
        models = self.settings.get("models") or {}
        return models.get(role) or self.settings.get("model", "")

    def chat(self, role, messages, tools=None, temperature=0.1):
        provider = self.provider_for(role)
        return provider.chat(
            messages,
            tools=tools,
            temperature=temperature,
            model=self.model_for(role),
        )
