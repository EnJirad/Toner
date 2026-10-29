"""Reviewer role: independent, diff-based review with a structured verdict."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from core.textutil import extract_json_object, truncate_middle

DECISIONS = ("approve", "reject", "needs_changes")
SEVERITIES = ("none", "low", "medium", "high", "critical")


@dataclass
class ReviewResult:
    decision: str
    issues: list = field(default_factory=list)
    severity: str = "none"
    raw: str = ""
    model: str = ""

    def to_dict(self):
        return {
            "decision": self.decision,
            "issues": self.issues,
            "severity": self.severity,
            "raw": self.raw[:4000],
            "model": self.model,
        }


class Reviewer:
    def __init__(self, settings, router, bus=None, redactor=None):
        self.settings = settings
        self.router = router
        self.bus = bus
        self.redactor = redactor

    def _diff(self, diff_text):
        limit = int(self.settings.get("max_review_diff_chars", 12000))
        text = self.redactor.redact(diff_text) if self.redactor else diff_text
        return truncate_middle(text or "(no diff available)", limit)

    def review(self, task, build, diff_text, changed_files):
        if self.bus is not None:
            self.bus.emit("review.started", task_id=task.id)

        prompt = f"""
Review the change below. You did not write it, so verify the claims yourself
against the diff, the changed file list and the commands that actually ran.

Task:
{json.dumps(
    {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "acceptance_criteria": task.acceptance_criteria,
        "checks": task.checks,
    },
    ensure_ascii=False,
    indent=2,
)}

Builder summary (untrusted claim):
{build.summary[:3000]}

Changed files reported by Git:
{json.dumps(changed_files, ensure_ascii=False, indent=2)}

Commands actually executed during the build, with exit codes:
{json.dumps(build.checks_run, ensure_ascii=False, indent=2)}

Diff:
```diff
{self._diff(diff_text)}
```

Check for:
- correctness and completeness against the task and acceptance criteria
- architecture and scope: unrelated changes, duplicated logic, API misuse
- security: secret exposure, disabled protections, dangerous commands
- unintended changes or regressions, including pre-existing user changes
- missing tests or checks that cannot actually pass

Reply with JSON only:
{{
  "decision": "approve|reject|needs_changes",
  "issues": ["specific issue, or empty"],
  "severity": "none|low|medium|high|critical"
}}

Rules:
- Do not approve if you cannot see evidence for the acceptance criteria.
- A builder summary is not evidence.
- Use "critical" or "high" for security problems or likely data loss.
""".strip()

        message = self.router.chat(
            "reviewer",
            [
                {
                    "role": "system",
                    "content": (
                        "You are a strict senior reviewer. Reply with a single "
                        "parseable JSON object and nothing else."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            tools=None,
            temperature=0,
        )

        raw = (message.get("content") or "").strip()
        result = self._parse(raw)

        if self.redactor is not None:
            result.raw = self.redactor.redact(result.raw)
            result.issues = [
                self.redactor.redact(item) if isinstance(item, str) else item
                for item in result.issues
            ]

        if self.bus is not None:
            self.bus.emit(
                "review.completed",
                task_id=task.id,
                decision=result.decision,
                severity=result.severity,
                issues=len(result.issues),
            )

        return result

    def _parse(self, raw):
        try:
            data = extract_json_object(raw)
        except ValueError:
            return ReviewResult(
                decision="needs_changes",
                issues=["Reviewer output was not parseable JSON; treated as needs_changes."],
                severity="medium",
                raw=raw,
            )

        decision = str(data.get("decision", "")).strip().lower()

        if decision not in DECISIONS:
            decision = "needs_changes"

        severity = str(data.get("severity", "")).strip().lower()

        if severity not in SEVERITIES:
            severity = "medium" if decision != "approve" else "none"

        issues = data.get("issues", [])

        if isinstance(issues, str):
            issues = [issues]

        if not isinstance(issues, list):
            issues = [str(issues)]

        return ReviewResult(
            decision=decision,
            issues=issues,
            severity=severity,
            raw=raw,
        )
