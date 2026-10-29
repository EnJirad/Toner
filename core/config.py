"""Centralized configuration loading for Toner.

Secrets come from the environment where possible, numeric limits are coerced
once, and every module reads the same resolved settings dictionary.
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

DEFAULTS = {
    "api_url": "",
    "api_key": "",
    "model": "",
    "provider": {"type": "openai_compatible"},
    "models": {},
    "project_root": "",
    "state_directory": ".toner",
    "max_tool_rounds": 50,
    "request_timeout": 120,
    "command_timeout": 180,
    "max_file_bytes": 100000,
    "max_search_results": 50,
    "max_list_files": 500,
    "max_auto_steps_per_session": 10,
    "max_plan_files": 100,
    "max_plan_tasks": 30,
    "max_context_chars": 24000,
    "max_command_output_chars": 15000,
    "max_review_diff_chars": 12000,
    "max_output_chars": 30000,
    "require_approval_for_writes": True,
    "require_approval_for_commands": True,
    "require_approval_for_deletions": True,
    "allow_file_deletion": False,
    "load_project_rules": True,
    "project_rule_files": [
        ".ai/AI_RULES.md",
        ".ai/AI_HANDOFF.md",
        ".ai/README.md",
        "AGENTS.md",
    ],
    "ignored_directories": [
        ".git",
        "node_modules",
        ".next",
        "dist",
        "build",
        "coverage",
        ".venv",
        "venv",
        "__pycache__",
        ".turbo",
        ".cache",
    ],
    "max_attempts": 3,
    "max_identical_failures": 2,
    "retry_backoff_seconds": 2,
    "retry_backoff_max_seconds": 30,
    "auto_commit": False,
    "require_acceptance_checks": True,
    "batch_limits": {"low": 20, "medium": 5, "high": 1},
    "test_command": "",
}

ENV_OVERRIDES = {
    "TONER_PROJECT_ROOT": "project_root",
    "TONER_API_URL": "api_url",
    "TONER_MODEL": "model",
    "TONER_STATE_DIR": "state_directory",
}

INT_KEYS = (
    "max_tool_rounds",
    "request_timeout",
    "command_timeout",
    "max_file_bytes",
    "max_search_results",
    "max_list_files",
    "max_auto_steps_per_session",
    "max_plan_files",
    "max_plan_tasks",
    "max_context_chars",
    "max_command_output_chars",
    "max_review_diff_chars",
    "max_output_chars",
    "max_attempts",
    "max_identical_failures",
    "retry_backoff_seconds",
    "retry_backoff_max_seconds",
)


class SettingsError(ValueError):
    pass


def default_settings():
    """Return an independent copy of the built-in defaults."""
    return copy.deepcopy(DEFAULTS)


def load_settings(settings_path=None, environ=None):
    """Load, merge and validate settings.

    The API key is *not* required here: read-only commands such as /status,
    /resume and /report must work during recovery even without credentials.
    Missing credentials fail only when a model call is actually attempted.
    """
    env = os.environ if environ is None else environ

    if settings_path is None:
        configured_path = env.get("TONER_SETTINGS")
        settings_path = (
            Path(configured_path)
            if configured_path
            else Path(__file__).resolve().parent.parent / "settings.json"
        )

    settings_path = Path(settings_path)

    if not settings_path.is_file():
        raise SettingsError(f"Settings file not found: {settings_path}")

    try:
        loaded = json.loads(settings_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SettingsError(f"settings.json is not valid JSON: {exc}") from exc

    if not isinstance(loaded, dict):
        raise SettingsError("settings.json must contain a JSON object")

    settings = default_settings()
    settings.update(loaded)

    for env_key, setting_key in ENV_OVERRIDES.items():
        if env.get(env_key):
            settings[setting_key] = env[env_key]

    settings["api_key"] = (
        env.get("AIUNLIMIT_API_KEY") or settings.get("api_key") or ""
    ).strip()

    for key in INT_KEYS:
        try:
            settings[key] = int(settings.get(key, DEFAULTS[key]))
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} must be an integer") from exc

    # api_url/model are validated when a model call is attempted so read-only
    # recovery commands keep working with a partial configuration.
    if not settings.get("project_root"):
        raise SettingsError("project_root must be set in settings.json")

    project_root = Path(str(settings["project_root"])).expanduser().resolve()

    if not project_root.is_dir():
        raise SettingsError(f"project_root not found: {project_root}")

    settings["project_root"] = str(project_root)

    state_directory = Path(str(settings.get("state_directory", ".toner")))

    if state_directory.is_absolute():
        raise SettingsError("state_directory must be a relative path")

    if ".." in state_directory.parts:
        raise SettingsError("state_directory must stay inside project_root")

    settings["state_directory"] = str(state_directory)

    limits = settings.get("batch_limits")

    if not isinstance(limits, dict):
        limits = dict(DEFAULTS["batch_limits"])

    merged_limits = dict(DEFAULTS["batch_limits"])
    for risk, value in limits.items():
        try:
            merged_limits[str(risk)] = max(1, int(value))
        except (TypeError, ValueError):
            continue

    settings["batch_limits"] = merged_limits

    if not isinstance(settings.get("models"), dict):
        settings["models"] = {}

    if not isinstance(settings.get("provider"), dict):
        settings["provider"] = {"type": "openai_compatible"}

    settings["provider"].setdefault("type", "openai_compatible")
    settings["provider"].setdefault("api_url", settings["api_url"])
    settings["provider"].setdefault("model", settings["model"])

    return settings
