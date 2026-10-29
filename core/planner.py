"""Planner role: turn Markdown plans into validated, checkable tasks."""

from __future__ import annotations

import hashlib
from pathlib import Path

from core.textutil import extract_json_object

HIGH_RISK_KEYWORDS = (
    "auth",
    "login",
    "password",
    "permission",
    "authorization",
    "payment",
    "billing",
    "invoice",
    "migration",
    "database",
    "schema",
    "security",
    "token",
    "production",
    "deploy",
    "financial",
    "crypto",
    "secret",
    "credential",
)

MEDIUM_RISK_KEYWORDS = (
    "api",
    "config",
    "dependency",
    "model",
    "refactor",
    "integration",
)

CHECK_TYPES = ("command", "file_exists", "file_absent", "text_contains")

ALLOWED_CHECK_COMMANDS = (
    "python -m py_compile",
    "python -m pytest",
    "python -m unittest",
    "python -m compileall",
    "git diff --check",
    "npm test",
    "npm run test",
    "npm run lint",
    "bun test",
    "go test",
    "cargo test",
    "ruff check",
    "mypy",
)


class PlannerError(RuntimeError):
    pass


def infer_risk(*texts):
    haystack = " ".join(str(text or "") for text in texts).lower()

    if any(keyword in haystack for keyword in HIGH_RISK_KEYWORDS):
        return "high"

    if any(keyword in haystack for keyword in MEDIUM_RISK_KEYWORDS):
        return "medium"

    return "low"


class Planner:
    def __init__(self, settings, router, bus=None):
        self.settings = settings
        self.router = router
        self.bus = bus

    # -- plan files ------------------------------------------------------

    def read_plan_files(self, plans_dir):
        plans_dir = Path(plans_dir)
        limit = int(self.settings.get("max_plan_files", 100))
        files = []

        if not plans_dir.is_dir():
            return files

        for path in sorted(plans_dir.rglob("*.md")):
            if len(files) >= limit:
                break

            relative = path.relative_to(plans_dir)

            if any(part.startswith(".") for part in relative.parts):
                continue

            if path.is_symlink() or not path.is_file():
                continue

            try:
                path.resolve().relative_to(plans_dir.resolve())
            except ValueError:
                continue

            try:
                if path.stat().st_size > int(self.settings.get("max_file_bytes", 100000)):
                    print(f"Skipping oversized plan file: {path.name}")
                    continue
            except OSError:
                continue

            files.append(path)

        return files

    def fingerprint(self, files, base_dir):
        digest = hashlib.sha256()
        base = Path(base_dir)

        for path in files:
            digest.update(str(Path(path).relative_to(base)).encode("utf-8"))
            digest.update(Path(path).read_bytes())

        return digest.hexdigest()

    def read_plan_text(self, files):
        sections = []

        for path in files:
            try:
                sections.append(
                    f"\n--- {path.name} ---\n"
                    + path.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError):
                continue

        return "\n".join(sections)

    # -- planning --------------------------------------------------------

    def plan(self, plan_text):
        max_tasks = int(self.settings.get("max_plan_tasks", 30))

        prompt = f"""
Read the Markdown plan below and produce a task list as JSON only.

Response shape:
{{
  "tasks": [
    {{
      "id": "T001",
      "title": "Short title",
      "description": "What must change and why",
      "dependencies": [],
      "risk": "low|medium|high",
      "acceptance_criteria": [
        "Observable criterion stated so a human can check it"
      ],
      "checks": [
        {{"type": "command", "command": "python -m pytest tests/test_x.py"}},
        {{"type": "file_exists", "path": "src/module.py"}},
        {{"type": "text_contains", "path": "src/module.py", "text": "def handler("}}
      ],
      "verification": "How to verify this task"
    }}
  ]
}}

Rules:
- Order tasks by dependency and split large work into small tasks.
- Do not invent work that is not in the plan.
- Every task MUST include at least one machine-checkable item in "checks".
- "command" checks must be simple commands without shell operators, using one
  of these prefixes: {", ".join(ALLOWED_CHECK_COMMANDS)}.
- "file_exists", "file_absent" and "text_contains" checks are evaluated
  directly against project files.
- Do not claim that tests pass; you are only planning them.
- Mark authentication, payments, migrations, security or production work as
  high risk.
- Maximum {max_tasks} tasks. No Markdown fences. No prose outside the JSON.

Plan:
{plan_text}
""".strip()

        response = self.router.chat(
            "planner",
            [
                {
                    "role": "system",
                    "content": (
                        "You are a precise software planner. Reply with a single "
                        "parseable JSON object and nothing else."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
            temperature=0.1,
        )

        content = (response.get("content") or "").strip()

        try:
            data = extract_json_object(content)
        except ValueError as exc:
            raise PlannerError(f"Planner did not return valid JSON: {exc}") from exc

        tasks = data.get("tasks")

        if not isinstance(tasks, list) or not tasks:
            raise PlannerError("Planner did not return a non-empty 'tasks' list")

        if len(tasks) > max_tasks:
            raise PlannerError(
                f"Planner returned {len(tasks)} tasks, above max_plan_tasks={max_tasks}"
            )

        normalised = self._normalise_tasks(tasks)

        if self.bus is not None:
            self.bus.emit("plan.created", tasks=len(normalised))

        return normalised

    def _normalise_tasks(self, tasks):
        seen = set()
        normalised = []

        for index, raw in enumerate(tasks, 1):
            if not isinstance(raw, dict):
                raise PlannerError(f"Task #{index} is not a JSON object")

            task_id = str(raw.get("id") or f"T{index:03d}").strip()

            if not task_id:
                raise PlannerError(f"Task #{index} has an empty id")

            if task_id in seen:
                raise PlannerError(f"Duplicate task id: {task_id}")

            seen.add(task_id)

            criteria = raw.get("acceptance_criteria", [])

            if not isinstance(criteria, list):
                raise PlannerError(f"acceptance_criteria must be a list in {task_id}")

            dependencies = raw.get("dependencies", [])

            if not isinstance(dependencies, list):
                raise PlannerError(f"dependencies must be a list in {task_id}")

            title = str(raw.get("title", "") or "").strip()
            description = str(raw.get("description", "") or "").strip()
            risk = str(raw.get("risk", "") or "").lower()

            if risk not in ("low", "medium", "high"):
                risk = infer_risk(title, description)

            normalised.append(
                {
                    "id": task_id,
                    "title": title or task_id,
                    "description": description,
                    "dependencies": [str(dep) for dep in dependencies],
                    "acceptance_criteria": [str(item) for item in criteria],
                    "checks": self._normalise_checks(raw.get("checks", []), task_id),
                    "verification": str(raw.get("verification", "") or ""),
                    "risk": risk,
                }
            )

        known = {task["id"] for task in normalised}

        for task in normalised:
            unknown = set(task["dependencies"]) - known

            if unknown:
                raise PlannerError(
                    f"Task {task['id']} depends on unknown tasks: {sorted(unknown)}"
                )

        return normalised

    @staticmethod
    def _normalise_checks(raw_checks, task_id):
        if raw_checks is None:
            return []

        if not isinstance(raw_checks, list):
            raise PlannerError(f"checks must be a list in {task_id}")

        checks = []

        for raw in raw_checks:
            if not isinstance(raw, dict):
                raise PlannerError(f"Each check must be an object in {task_id}")

            kind = str(raw.get("type", "")).strip()

            if kind not in CHECK_TYPES:
                raise PlannerError(f"Unknown check type {kind!r} in {task_id}")

            if kind == "command":
                command = str(raw.get("command", "") or "").strip()

                if not command:
                    raise PlannerError(f"command check without a command in {task_id}")

                check = {"type": "command", "command": command}

                if raw.get("golden"):
                    check["golden"] = True

            elif kind in ("file_exists", "file_absent"):
                path = str(raw.get("path", "") or "").strip()

                if not path:
                    raise PlannerError(f"{kind} check without a path in {task_id}")

                check = {"type": kind, "path": path}

            else:
                path = str(raw.get("path", "") or "").strip()
                text = raw.get("text")

                if not path or not isinstance(text, str) or not text:
                    raise PlannerError(
                        f"text_contains check needs path and text in {task_id}"
                    )

                check = {"type": kind, "path": path, "text": text}

            checks.append(check)

        return checks
