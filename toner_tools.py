
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
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
    r"\bmkfs(?:\.\w+)?\b",
    r"\bdd\s+if\s*=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bDROP\s+DATABASE\b",
    r"\bTRUNCATE\s+TABLE\b",
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


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class AgentTools:
    def __init__(self, settings):
        self.settings = settings
        self.root = Path(
            settings["project_root"]
        ).expanduser().resolve()

        if not self.root.is_dir():
            raise ValueError(
                f"ไม่พบโฟลเดอร์โปรเจกต์: {self.root}"
            )

        self.state_root = (
            self.root
            / settings.get("state_directory", ".toner")
        ).resolve()

        try:
            self.state_root.relative_to(self.root)
        except ValueError:
            raise ValueError(
                "state_directory ต้องอยู่ภายใน project_root"
            )

        self.state_root.mkdir(parents=True, exist_ok=True)
        self.state_root.joinpath("files_md").mkdir(
            parents=True, exist_ok=True
        )

        self.recheck_root = self.state_root / "ai_recheck"
        self.recheck_root.mkdir(parents=True, exist_ok=True)

        for name in ("checkpoints", "reports", "backups"):
            (self.recheck_root / name).mkdir(
                parents=True, exist_ok=True
            )

        command_file = self.state_root / "new_command.md"
        if not command_file.exists():
            command_file.write_text(
                "# New Command\n\n"
                "เขียนงานที่ต้องการให้ Toner ทำไว้ในไฟล์นี้\n"
                "ระบุเป้าหมาย ขอบเขต และวิธีตรวจสอบผลลัพธ์\n",
                encoding="utf-8",
            )

        self.max_file_bytes = int(
            settings.get("max_file_bytes", 100000)
        )
        self.ignored_directories = set(
            settings.get("ignored_directories", [])
        )
        self.ignored_directories.add(".git")

    @staticmethod
    def is_secret(path):
        path = Path(path)
        name = path.name.lower()

        if name in SECRET_NAMES:
            return True

        if name.startswith(".env.") and name != ".env.example":
            return True

        return path.suffix.lower() in SECRET_SUFFIXES

    def path(self, relative):
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("กรุณาระบุ path")

        raw = Path(relative)

        if raw.is_absolute():
            raise ValueError("ต้องใช้ relative path ภายในโปรเจกต์")

        target = (self.root / raw).resolve()

        try:
            target.relative_to(self.root)
        except ValueError:
            raise ValueError("ปฏิเสธ path ที่อยู่นอก project_root")

        # ป้องกัน AI อ่านไฟล์สำรองของ Toner โดยตรง
        backup_root = (
            self.recheck_root / "backups"
        ).resolve()

        try:
            target.relative_to(backup_root)
        except ValueError:
            pass
        else:
            raise ValueError(
                "ปฏิเสธการเข้าถึงไฟล์สำรองภายใน Toner"
            )

        # ป้องกันการเข้าถึงไฟล์ความลับ
        if self.is_secret(target):
            raise ValueError("ปฏิเสธการเข้าถึงไฟล์ความลับ")

        return target

    def ask(self, message, always=False):
        if not always and not self.settings.get(
            "require_approval_for_writes", True
        ):
            return True

        try:
            answer = input(
                f"\n⚠️ {message} [y/N]: "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nยกเลิกการดำเนินการ")
            return False

        return answer in ("y", "yes")

    def _ignored(self, path):
        return any(
            part in self.ignored_directories
            for part in Path(path).parts
        )

    def _walk_files(self, base):
        for current, dirs, files in os.walk(base, followlinks=False):
            current_path = Path(current)

            dirs[:] = sorted(
                name for name in dirs
                if name not in self.ignored_directories
                and not (current_path / name).is_symlink()
            )

            for filename in sorted(files):
                item = current_path / filename

                if item.is_symlink():
                    continue
                if self.is_secret(item):
                    continue
                if self._ignored(item.relative_to(self.root)):
                    continue

                try:
                    item.resolve().relative_to(self.root)
                except ValueError:
                    continue

                yield item

    def list_files(self, directory="."):
        base = self.path(directory)

        if not base.is_dir():
            return {"error": "ไม่ใช่โฟลเดอร์"}

        results = []
        limit = int(self.settings.get("max_list_files", 500))

        for path in self._walk_files(base):
            results.append(
                str(path.relative_to(self.root))
            )

            if len(results) >= limit:
                return {
                    "files": results,
                    "truncated": True,
                }

        return {"files": results, "truncated": False}

    def read_file(self, path, start_line=1, end_line=None):
        target = self.path(path)

        if not target.is_file():
            return {"error": "ไม่พบไฟล์"}

        if target.stat().st_size > self.max_file_bytes:
            return {
                "error": "ไฟล์ใหญ่เกินกำหนด กรุณาเพิ่ม max_file_bytes หรืออ่านผ่านคำสั่งเฉพาะ"
            }

        try:
            lines = target.read_text(
                encoding="utf-8"
            ).splitlines()

            start = max(1, int(start_line or 1))
            end = min(
                len(lines),
                int(end_line) if end_line is not None else len(lines),
            )

            content = "\n".join(
                f"{i}: {lines[i - 1]}"
                for i in range(start, end + 1)
            ) if start <= end else ""

            return {
                "path": str(target.relative_to(self.root)),
                "total_lines": len(lines),
                "content": content,
            }
        except UnicodeDecodeError:
            return {"error": "ไฟล์ไม่ใช่ข้อความ UTF-8"}
        except Exception as exc:
            return {"error": str(exc)}

    def search_code(self, query, directory="."):
        if not isinstance(query, str) or not query:
            return {"error": "query ต้องไม่ว่าง"}

        base = self.path(directory)

        if not base.is_dir():
            return {"error": "directory ไม่ใช่โฟลเดอร์"}

        max_results = int(
            self.settings.get("max_search_results", 50)
        )
        matches = []

        for path in self._walk_files(base):
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue

                lines = path.read_text(
                    encoding="utf-8"
                ).splitlines()

                for number, line in enumerate(lines, 1):
                    if query.casefold() in line.casefold():
                        matches.append({
                            "file": str(path.relative_to(self.root)),
                            "line": number,
                            "text": line[:300],
                        })

                        if len(matches) >= max_results:
                            return {
                                "matches": matches,
                                "truncated": True,
                            }
            except (UnicodeDecodeError, OSError):
                continue

        return {"matches": matches, "truncated": False}

    def _backup(self, target):
        relative = target.relative_to(self.root)
        backup_name = (
            datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            + "__"
            + str(relative).replace("/", "__")
        )
        backup = self.recheck_root / "backups" / backup_name
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        return str(backup.relative_to(self.root))

    def write_file(self, path, content):
        target = self.path(path)

        if target.exists() and target.is_dir():
            return {"error": "path เป็นโฟลเดอร์"}

        if not isinstance(content, str):
            return {"error": "content ต้องเป็นข้อความ"}

        if len(content.encode("utf-8")) > self.max_file_bytes:
            return {"error": "เนื้อหาไฟล์ใหญ่เกินกำหนด"}

        existed = target.is_file()
        print(f"\n📝 {'เขียนทับ' if existed else 'สร้าง'}: {path}")

        if not self.ask("อนุมัติการเขียนไฟล์"):
            return {"cancelled": True}

        backup = None

        try:
            if existed:
                backup = self._backup(target)

            target.parent.mkdir(parents=True, exist_ok=True)

            # เขียนไฟล์ชั่วคราวก่อน แล้วแทนที่เพื่อลดความเสี่ยงไฟล์เสีย
            temp = target.with_name(target.name + ".toner-tmp")
            temp.write_text(content, encoding="utf-8")
            temp.replace(target)

            return {
                "success": True,
                "path": str(target.relative_to(self.root)),
                "backup": backup,
            }
        except Exception as exc:
            return {"error": str(exc), "backup": backup}

    def edit_file(self, path, old_text, new_text):
        target = self.path(path)

        if not target.is_file():
            return {"error": "ไม่พบไฟล์"}

        if target.stat().st_size > self.max_file_bytes:
            return {"error": "ไฟล์ใหญ่เกินกำหนด"}

        if not old_text:
            return {"error": "old_text ต้องไม่ว่าง"}

        try:
            original = target.read_text(encoding="utf-8")
            count = original.count(old_text)

            if count != 1:
                return {
                    "error": (
                        f"พบข้อความเดิม {count} จุด "
                        "ต้องพบเพียง 1 จุดเพื่อป้องกันแก้ผิดตำแหน่ง"
                    )
                }

            updated = original.replace(old_text, new_text, 1)

            if len(updated.encode("utf-8")) > self.max_file_bytes:
                return {"error": "ไฟล์หลังแก้ไขใหญ่เกินกำหนด"}

            print(f"\n✏️ แก้ไข: {path}")

            if not self.ask("อนุมัติการแก้ไขไฟล์"):
                return {"cancelled": True}

            backup = self._backup(target)
            temp = target.with_name(target.name + ".toner-tmp")
            temp.write_text(updated, encoding="utf-8")
            temp.replace(target)

            return {
                "success": True,
                "path": str(target.relative_to(self.root)),
                "backup": backup,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def delete_file(self, path):
        if not self.settings.get("allow_file_deletion", False):
            return {"error": "ปิด allow_file_deletion ใน settings.json"}

        target = self.path(path)

        if not target.is_file():
            return {"error": "ไม่พบไฟล์หรือ path ไม่ใช่ไฟล์"}

        print(f"\n🗑️ ลบไฟล์: {path}")

        if not self.settings.get("require_approval_for_deletions", True):
            allowed = self.ask("ยืนยันการลบไฟล์", always=True)
        else:
            allowed = self.ask("ยืนยันการลบไฟล์", always=True)

        if not allowed:
            return {"cancelled": True}

        try:
            backup = self._backup(target)
            target.unlink()

            return {
                "success": True,
                "deleted": str(target.relative_to(self.root)),
                "backup": backup,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def run_command(self, command):
        if not isinstance(command, str) or not command.strip():
            return {"error": "command ต้องไม่ว่าง"}

        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return {
                    "error": (
                        "บล็อกคำสั่งที่มีความเสี่ยงสูง "
                        "ให้ดำเนินการด้วยตนเองนอก Toner"
                    )
                }

        if "\x00" in command:
            return {"error": "คำสั่งมีอักขระที่ไม่อนุญาต"}

        print(f"\n💻 คำสั่ง: {command}")

        if self.settings.get("require_approval_for_commands", True):
            if not self.ask("อนุญาตให้รันคำสั่งนี้"):
                return {"cancelled": True}

        env = os.environ.copy()

        for key in list(env):
            if any(word in key.upper() for word in SECRET_ENV_WORDS):
                env.pop(key, None)

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=str(self.root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
            )

            output = (
                (result.stdout or "")
                + "\n"
                + (result.stderr or "")
            ).strip()

            return {
                "returncode": result.returncode,
                "output": output[-15000:],
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")

            return {
                "error": "คำสั่งเกินเวลาที่กำหนด",
                "partial_output": str(partial)[-3000:],
                "timed_out": True,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def git_status(self):
        return self._git(["status", "--short", "--branch"])

    def git_diff(self):
        return self._git(["diff", "--"])

    def _git(self, args):
        if shutil.which("git") is None:
            return {"error": "ไม่พบคำสั่ง git ใน PATH"}

        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
            return {
                "returncode": result.returncode,
                "output": result.stdout[-15000:],
                "error": result.stderr[-3000:],
            }
        except Exception as exc:
            return {"error": str(exc)}

    def execute(self, name, args):
        methods = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_code": self.search_code,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "delete_file": self.delete_file,
            "run_command": self.run_command,
            "git_status": self.git_status,
            "git_diff": self.git_diff,
        }

        method = methods.get(name)

        if method is None:
            return {"error": f"ไม่รู้จักเครื่องมือ: {name}"}

        if not isinstance(args, dict):
            return {"error": "tool arguments ต้องเป็น JSON object"}

        try:
            return method(**args)
        except TypeError as exc:
            return {"error": f"arguments ไม่ถูกต้อง: {exc}"}
        except Exception as exc:
            return {"error": str(exc)}
