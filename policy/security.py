"""Centralized security policy for Toner.

Every filesystem, shell and git operation goes through this module. The
original protections (project-root containment, secret-file blocking, backup
isolation, dangerous-command blocking, secret environment filtering) are
preserved and extended with secret-value redaction and a strict allowlist for
automated QA checks.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SECRET_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
}

SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}

DANGEROUS_PATTERNS = [
    r"\brm\s+-[^\n]*r[^\n]*f",
    r"\bgit\s+reset\s+--hard\b",
    r"\bgit\s+clean\s+-[^\n]*f",
    r"\bgit\s+push\b[^\n]*(--force\b|-f\b)",
    r"\bgit\s+restore\b",
    r"\bgit\s+checkout\s+--\s+\.",
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\s+if\s*=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\s+TABLE\b",
    r"\bchmod\s+-R\s+777\b",
    r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:ba|z)?sh\b",
]

SECRET_ENV_WORDS = (
    "API_KEY",
    "API_TOKEN",
    "SECRET",
    "PASSWORD",
    "DATABASE_URL",
    "PRIVATE_KEY",
    "ACCESS_TOKEN",
    "AUTH_TOKEN",
)

# Commands that automated QA is allowed to run without human approval. Each
# entry must be a simple command: shell metacharacters are rejected outright.
SAFE_CHECK_PREFIXES = (
    "python -m py_compile",
    "python3 -m py_compile",
    "python -m compileall",
    "python3 -m compileall",
    "python -m pytest",
    "python3 -m pytest",
    "python -m unittest",
    "python3 -m unittest",
    "pytest",
    "git diff --check",
    "git status",
    "git diff",
    "npm test",
    "npm run test",
    "npm run lint",
    "bun test",
    "go test",
    "cargo test",
    "ruff check",
    "mypy",
)

SHELL_METACHARACTERS = (";", "|", "&", "`", "$", ">", "<", "\n", "\r")

_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")
_SK_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")
_ASSIGN_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd)"
    r"(\s*[=:]\s*)"
    r"([^\s'\"]{8,})"
)
_URL_CREDENTIALS_RE = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")


class SecurityError(ValueError):
    """A policy denial with a stable machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def is_secret(path):
    """Return True when ``path`` names a file Toner must never expose."""
    path = Path(path)
    name = path.name.lower()

    if name in SECRET_NAMES:
        return True

    if name.startswith(".env.") and name != ".env.example":
        return True

    return path.suffix.lower() in SECRET_SUFFIXES


class SecretRedactor:
    """Remove secret values from text before it is logged or stored."""

    def __init__(self, values=None):
        unique = {
            value
            for value in (values or [])
            if isinstance(value, str) and len(value) >= 8
        }
        self._values = sorted(unique, key=len, reverse=True)

    @classmethod
    def from_environment(cls, environ=None):
        env = os.environ if environ is None else environ
        values = []
        for key, value in env.items():
            if any(word in key.upper() for word in SECRET_ENV_WORDS):
                values.append(value)
        return cls(values)

    def redact(self, text):
        if not isinstance(text, str) or not text:
            return text

        result = text
        for value in self._values:
            result = result.replace(value, "[REDACTED]")

        result = _BEARER_RE.sub("Bearer [REDACTED]", result)
        result = _SK_RE.sub("[REDACTED]", result)
        result = _ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", result)
        result = _URL_CREDENTIALS_RE.sub(r"\1[REDACTED]:[REDACTED]@", result)
        return result


class SecurityPolicy:
    """Resolved project boundaries, limits and secret handling."""

    def __init__(self, settings, root=None):
        self.settings = settings
        configured = root if root is not None else settings.get("project_root")

        if not configured:
            raise SecurityError(
                "project_root_missing",
                "project_root is required in settings.json",
            )

        self.root = Path(configured).expanduser().resolve()

        if not self.root.is_dir():
            raise SecurityError(
                "project_root_missing",
                f"Project root not found: {self.root}",
            )

        state_directory = Path(str(settings.get("state_directory", ".toner")))

        if state_directory.is_absolute():
            raise SecurityError(
                "invalid_state_directory",
                "state_directory must be a relative path inside project_root",
            )

        self.state_root = (self.root / state_directory).resolve()

        try:
            self.state_root.relative_to(self.root)
        except ValueError:
            raise SecurityError(
                "invalid_state_directory",
                "state_directory must stay inside project_root",
            )

        self.recheck_root = self.state_root / "ai_recheck"
        self.backup_root = self.recheck_root / "backups"

        self.max_file_bytes = max(1, int(settings.get("max_file_bytes", 100000)))
        self.max_search_results = max(1, int(settings.get("max_search_results", 50)))
        self.max_list_files = max(1, int(settings.get("max_list_files", 500)))
        self.command_timeout = max(1, int(settings.get("command_timeout", 180)))
        self.max_command_output_chars = max(
            1000, int(settings.get("max_command_output_chars", 15000))
        )
        self.max_output_limit = max(1000, int(settings.get("max_output_chars", 30000)))

        self.ignored_directories = set(settings.get("ignored_directories", []))
        self.ignored_directories.add(".git")

        self.redactor = SecretRedactor.from_environment()

    # -- workspace -------------------------------------------------------

    def ensure_workspace(self):
        """Create the Toner state tree and the starter command file."""
        for path in (
            self.state_root,
            self.state_root / "files_md",
            self.state_root / "audit",
            self.recheck_root,
            self.backup_root,
            self.recheck_root / "reports",
        ):
            path.mkdir(parents=True, exist_ok=True)

        command_file = self.state_root / "new_command.md"

        if not command_file.exists():
            command_file.write_text(
                "# New Command\n\n"
                "Write the task you want Toner to perform in this file.\n"
                "State the goal, the scope, and how the result should be verified.\n",
                encoding="utf-8",
            )

    # -- paths -----------------------------------------------------------

    def resolve(self, relative):
        """Resolve a relative path and enforce every containment rule."""
        if not isinstance(relative, str) or not relative.strip():
            raise SecurityError("invalid_path", "A non-empty relative path is required")

        raw = Path(relative)

        if raw.is_absolute():
            raise SecurityError(
                "absolute_path",
                "Use a relative path inside project_root",
            )

        target = (self.root / raw).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise SecurityError(
                "path_outside_root",
                "Rejected a path outside project_root",
            )

        try:
            target.relative_to(self.backup_root)
        except ValueError:
            pass
        else:
            raise SecurityError(
                "backup_access",
                "Access to Toner backup files is denied",
            )

        try:
            target.relative_to(self.state_root)
        except ValueError:
            pass
        else:
            raise SecurityError(
                "state_access",
                "Tasks may not read or modify Toner's own state directory",
            )

        parts = target.relative_to(self.root).parts

        if parts and parts[0] == ".git":
            raise SecurityError(
                "git_internal",
                "Access to .git internals is denied",
            )

        if is_secret(target):
            raise SecurityError(
                "secret_file",
                "Access to secret files is denied",
            )

        return target

    def relative(self, path):
        return str(Path(path).resolve().relative_to(self.root))

    def is_ignored(self, relative):
        return any(part in self.ignored_directories for part in Path(relative).parts)

    # -- environment -----------------------------------------------------

    def filter_env(self, environ=None):
        """Copy the environment without secrets or Toner's own API key."""
        source = os.environ if environ is None else environ
        filtered = {}

        for key, value in source.items():
            upper = key.upper()
            if any(word in upper for word in SECRET_ENV_WORDS):
                continue
            if upper.startswith("TONER_"):
                continue
            filtered[key] = value

        return filtered

    # -- commands --------------------------------------------------------

    def screen_command(self, command):
        """Raise :class:`SecurityError` for dangerous or malformed commands."""
        if not isinstance(command, str) or not command.strip():
            raise SecurityError("invalid_command", "command must be a non-empty string")

        if "\x00" in command:
            raise SecurityError(
                "invalid_command",
                "command contains a disallowed null byte",
            )

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                raise SecurityError(
                    "dangerous_command",
                    "Blocked a high-risk command; run it manually outside Toner",
                )

    def is_safe_check(self, command):
        """Return True when automated QA may run ``command`` unattended."""
        if not isinstance(command, str):
            return False

        command = command.strip()

        if not command or "\x00" in command:
            return False

        if any(character in command for character in SHELL_METACHARACTERS):
            return False

        # QA invokes the current interpreter explicitly so it works where
        # ``python`` is not on PATH. Normalise it back for the allowlist.
        executable = sys.executable

        if executable and command.startswith(executable + " "):
            command = "python" + command[len(executable):]

        if not any(
            command == prefix or command.startswith(prefix + " ")
            for prefix in SAFE_CHECK_PREFIXES
        ):
            return False

        tokens = command.split()

        for token in tokens[1:]:
            if token.startswith("-"):
                continue
            normalised = token.replace("\\", "/")
            if normalised.startswith("/") or normalised.startswith("~"):
                return False
            if ".." in normalised.split("/"):
                return False

        return True


class ApprovalPolicy:
    """Interactive approval gate preserving the original prompt behaviour."""

    def __init__(self, settings, input_fn=None, output_fn=None):
        self.settings = settings
        self._input = input_fn or input
        self._output = output_fn or print

    def confirm(self, message, always=False, setting="require_approval_for_writes"):
        if not always and not self.settings.get(setting, True):
            return True

        try:
            answer = self._input(f"\n⚠️ {message} [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self._output("\nCancelled.")
            return False

        return answer in ("y", "yes")
