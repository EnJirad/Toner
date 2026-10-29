"""Tool registry: the single contract for tool schemas and dispatch.

The model may only call tools listed in :func:`make_tool_schema`, and every
call passes through :meth:`ToolRegistry.execute`, which applies the security
policy before any side effect. Git commits are intentionally *not* exposed to
the model: only the orchestrator may commit, and only after verification.
"""

from __future__ import annotations

from pathlib import Path

from policy.security import ApprovalPolicy, SecurityError, SecurityPolicy, is_secret
from tools.filesystem import FileSystemTools
from tools.git import GitTools
from tools.search import SearchTools
from tools.shell import ShellTools


def make_tool_schema():
    """Return the OpenAI-compatible tool schema for the allowed tools."""

    def schema(name, description, properties, required=None):
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                    "additionalProperties": False,
                },
            },
        }

    string = {"type": "string"}

    return [
        schema(
            "list_files",
            "List project files under a directory.",
            {"directory": string},
            ["directory"],
        ),
        schema(
            "read_file",
            "Read a UTF-8 text file, optionally limited to a line range.",
            {
                "path": string,
                "start_line": {"type": "integer"},
                "end_line": {"type": ["integer", "null"]},
            },
            ["path"],
        ),
        schema(
            "search_code",
            "Search for a literal string in project files.",
            {"query": string, "directory": string},
            ["query"],
        ),
        schema(
            "write_file",
            "Create or overwrite a file after approval.",
            {"path": string, "content": string},
            ["path", "content"],
        ),
        schema(
            "edit_file",
            "Replace exactly one matching occurrence in an existing file.",
            {"path": string, "old_text": string, "new_text": string},
            ["path", "old_text", "new_text"],
        ),
        schema(
            "delete_file",
            "Delete a file after explicit approval.",
            {"path": string},
            ["path"],
        ),
        schema(
            "run_command",
            "Run a screened shell command inside project_root after approval.",
            {"command": string},
            ["command"],
        ),
        schema("git_status", "Show Git status.", {}, []),
        schema("git_diff", "Show unstaged Git diff.", {}, []),
        schema("git_log", "Show recent Git commits.", {}, []),
        schema("git_branch", "Show the current Git branch.", {}, []),
    ]


class ToolRegistry:
    """Facade over the concrete tool implementations.

    Also serves as the backward-compatible ``AgentTools`` API: existing callers
    that construct it with a settings dict and call ``execute`` keep working.
    """

    def __init__(self, settings, policy=None, approval=None):
        self.settings = settings
        self.policy = policy or SecurityPolicy(settings)
        self.approval = approval or ApprovalPolicy(settings)

        self.root = self.policy.root
        self.state_root = self.policy.state_root
        self.recheck_root = self.policy.recheck_root
        self.max_file_bytes = self.policy.max_file_bytes

        self.policy.ensure_workspace()

        self.filesystem = FileSystemTools(self.policy, self.approval)
        self.search = SearchTools(self.policy)
        self.shell = ShellTools(self.policy, self.approval)
        self.git = GitTools(self.root)

        self._dispatch = {
            "list_files": self.filesystem.list_files,
            "read_file": self.filesystem.read_file,
            "search_code": self.search.search_code,
            "write_file": self.filesystem.write_file,
            "edit_file": self.filesystem.edit_file,
            "delete_file": self.filesystem.delete_file,
            "run_command": self.shell.run,
            "git_status": self.git.status,
            "git_diff": self.git.diff,
            "git_log": self.git.log,
            "git_branch": self.git.branch,
        }

        self.schemas = make_tool_schema()

    # -- compatibility helpers ------------------------------------------

    def list_files(self, directory="."):
        return self.filesystem.list_files(directory)

    def read_file(self, path, start_line=1, end_line=None):
        return self.filesystem.read_file(path, start_line, end_line)

    def search_code(self, query, directory="."):
        return self.search.search_code(query, directory)

    def write_file(self, path, content):
        return self.filesystem.write_file(path, content)

    def edit_file(self, path, old_text, new_text):
        return self.filesystem.edit_file(path, old_text, new_text)

    def delete_file(self, path):
        return self.filesystem.delete_file(path)

    def run_command(self, command):
        return self.shell.run(command)

    def git_status(self):
        return self.git.status()

    def git_diff(self):
        return self.git.diff()

    @staticmethod
    def is_secret(path):
        return is_secret(path)

    def path(self, relative):
        return self.policy.resolve(relative)

    def ask(self, message, always=False):
        return self.approval.confirm(message, always=always)

    def tool_names(self):
        return sorted(self._dispatch)

    # -- dispatch --------------------------------------------------------

    def execute(self, name, arguments):
        if not isinstance(arguments, dict):
            return {
                "error": "tool arguments must be a JSON object",
                "code": "invalid_arguments",
            }

        method = self._dispatch.get(name)

        if method is None:
            return {
                "error": f"Unknown tool: {name}",
                "code": "unknown_tool",
            }

        try:
            result = method(**arguments)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}
        except TypeError as exc:
            return {
                "error": f"Invalid tool arguments: {exc}",
                "code": "invalid_arguments",
            }
        except Exception as exc:  # defensive: never crash the agent loop
            return {"error": str(exc), "code": "tool_failed"}

        if not isinstance(result, dict):
            return {"result": result}

        return result

    # -- git helpers used internally (not model tools) -------------------

    def git_current_sha(self):
        return self.git.current_sha()

    def git_changed_files(self):
        """Return Git changes excluding Toner's own state directory."""
        result = self.git.changed_files()

        if result.get("returncode") != 0:
            return result

        try:
            prefix = self.state_root.relative_to(self.root).parts
        except ValueError:
            prefix = ()

        project_files = []
        state_files = []

        for item in result.get("files", []):
            parts = Path(item).parts

            if prefix and parts[: len(prefix)] == prefix:
                state_files.append(item)
            else:
                project_files.append(item)

        result["files"] = project_files
        result["state_files"] = state_files
        return result

    def git_commit(self, message):
        return self.git.commit(message)
