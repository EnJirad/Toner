"""Test fixtures: isolated projects, settings and registries."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.config import default_settings
from policy.security import ApprovalPolicy
from tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]


@contextlib.contextmanager
def temp_dir():
    """Create an empty temporary directory."""
    path = Path(tempfile.mkdtemp(prefix="toner-test-"))

    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.contextmanager
def temp_project(files=None, git=True):
    """Create an isolated project directory, optionally a git repository."""
    root = Path(tempfile.mkdtemp(prefix="toner-test-"))

    try:
        if git:
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "toner@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Toner Test"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)

        for name, content in (files or {}).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def settings_for(root, **overrides):
    settings = default_settings()
    settings.update(
        {
            "project_root": str(root),
            "api_url": "http://api.invalid/v1/chat/completions",
            "api_key": "test-key-not-real",
            "model": "test-model",
            "require_approval_for_writes": False,
            "require_approval_for_commands": False,
            "require_approval_for_deletions": False,
            "allow_file_deletion": False,
            "retry_backoff_seconds": 0,
            "retry_backoff_max_seconds": 0,
            "max_auto_steps_per_session": 10,
            "auto_commit": False,
            "test_command": "",
        }
    )
    settings.update(overrides)
    return settings


def registry_for(settings, answer="y"):
    approval = ApprovalPolicy(
        settings,
        input_fn=lambda _prompt: answer,
        output_fn=lambda *_args, **_kwargs: None,
    )
    return ToolRegistry(settings, approval=approval)
