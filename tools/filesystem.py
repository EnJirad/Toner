"""Filesystem tools: every operation is constrained by the security policy."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from policy.security import SecurityError, is_secret
from core.textutil import timestamp_slug


def walk_files(policy, base):
    """Yield readable project files under ``base``, skipping ignored content."""
    root = policy.root

    for current, dirs, files in os.walk(base, followlinks=False):
        current_path = Path(current)

        dirs[:] = sorted(
            name
            for name in dirs
            if name not in policy.ignored_directories
            and not (current_path / name).is_symlink()
            and (current_path / name).resolve() != policy.state_root
        )

        for filename in sorted(files):
            item = current_path / filename

            if item.is_symlink():
                continue
            if is_secret(item):
                continue

            try:
                relative = item.relative_to(root)
            except ValueError:
                continue

            if policy.is_ignored(relative):
                continue

            try:
                item.resolve().relative_to(root)
            except ValueError:
                continue

            yield item


class FileSystemTools:
    def __init__(self, policy, approval):
        self.policy = policy
        self.approval = approval

    # -- reads -----------------------------------------------------------

    def list_files(self, directory="."):
        try:
            base = self.policy.resolve(directory)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if not base.is_dir():
            return {"error": "Not a directory", "code": "not_a_directory"}

        results = []

        for path in walk_files(self.policy, base):
            results.append(str(path.relative_to(self.policy.root)))

            if len(results) >= self.policy.max_list_files:
                return {"files": results, "truncated": True}

        return {"files": results, "truncated": False}

    def read_file(self, path, start_line=1, end_line=None):
        try:
            target = self.policy.resolve(path)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if not target.is_file():
            return {"error": "File not found", "code": "not_found"}

        if target.stat().st_size > self.policy.max_file_bytes:
            return {
                "error": (
                    "File is larger than max_file_bytes; "
                    "raise the limit or read it with a command"
                ),
                "code": "file_too_large",
            }

        try:
            lines = target.read_text(encoding="utf-8").splitlines()

            start = max(1, int(start_line or 1))
            end = min(
                len(lines),
                int(end_line) if end_line is not None else len(lines),
            )

            content = (
                "\n".join(
                    f"{index}: {lines[index - 1]}"
                    for index in range(start, end + 1)
                )
                if start <= end
                else ""
            )

            return {
                "path": str(target.relative_to(self.policy.root)),
                "total_lines": len(lines),
                "content": content,
            }
        except UnicodeDecodeError:
            return {"error": "File is not UTF-8 text", "code": "not_utf8"}
        except OSError as exc:
            return {"error": str(exc), "code": "read_failed"}

    # -- writes ----------------------------------------------------------

    def _backup(self, target):
        relative = target.relative_to(self.policy.root)
        backup_name = (
            timestamp_slug()
            + "__"
            + str(relative).replace("/", "__")
        )
        backup = self.policy.backup_root / backup_name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        return str(backup.relative_to(self.policy.root))

    def _write_atomic(self, target, content):
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(target.name + ".toner-tmp")
        temp.write_text(content, encoding="utf-8")
        temp.replace(target)

    def write_file(self, path, content):
        try:
            target = self.policy.resolve(path)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if target.exists() and target.is_dir():
            return {"error": "Path is a directory", "code": "path_is_directory"}

        if not isinstance(content, str):
            return {"error": "content must be a string", "code": "invalid_content"}

        if len(content.encode("utf-8")) > self.policy.max_file_bytes:
            return {
                "error": "Content exceeds max_file_bytes",
                "code": "file_too_large",
            }

        existed = target.is_file()
        print(f"\n📝 {'Overwrite' if existed else 'Create'}: {path}")

        if not self.approval.confirm(f"Write file {path}?"):
            return {"cancelled": True}

        backup = None

        try:
            if existed:
                backup = self._backup(target)

            self._write_atomic(target, content)

            return {
                "success": True,
                "path": str(target.relative_to(self.policy.root)),
                "backup": backup,
            }
        except OSError as exc:
            return {"error": str(exc), "code": "write_failed", "backup": backup}

    def edit_file(self, path, old_text, new_text):
        try:
            target = self.policy.resolve(path)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if not target.is_file():
            return {"error": "File not found", "code": "not_found"}

        if target.stat().st_size > self.policy.max_file_bytes:
            return {"error": "File is larger than max_file_bytes", "code": "file_too_large"}

        if not old_text:
            return {"error": "old_text must not be empty", "code": "invalid_edit"}

        if not isinstance(new_text, str):
            return {"error": "new_text must be a string", "code": "invalid_edit"}

        try:
            original = target.read_text(encoding="utf-8")
            count = original.count(old_text)

            if count != 1:
                return {
                    "error": (
                        f"Found {count} matches for old_text; exactly one is "
                        "required to avoid editing the wrong location"
                    ),
                    "code": "edit_not_unique",
                }

            updated = original.replace(old_text, new_text, 1)

            if len(updated.encode("utf-8")) > self.policy.max_file_bytes:
                return {
                    "error": "Edited file would exceed max_file_bytes",
                    "code": "file_too_large",
                }

            print(f"\n✏️ Edit: {path}")

            if not self.approval.confirm(f"Edit file {path}?"):
                return {"cancelled": True}

            backup = self._backup(target)
            self._write_atomic(target, updated)

            return {
                "success": True,
                "path": str(target.relative_to(self.policy.root)),
                "backup": backup,
            }
        except OSError as exc:
            return {"error": str(exc), "code": "write_failed"}

    def delete_file(self, path):
        if not self.policy.settings.get("allow_file_deletion", False):
            return {
                "error": "File deletion is disabled (allow_file_deletion=false)",
                "code": "deletion_disabled",
            }

        try:
            target = self.policy.resolve(path)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if not target.is_file():
            return {"error": "File not found", "code": "not_found"}

        print(f"\n🗑️ Delete: {path}")

        if not self.approval.confirm(f"Delete file {path}?", always=True):
            return {"cancelled": True}

        try:
            backup = self._backup(target)
            target.unlink()

            return {
                "success": True,
                "deleted": str(target.relative_to(self.policy.root)),
                "backup": backup,
            }
        except OSError as exc:
            return {"error": str(exc), "code": "delete_failed"}
