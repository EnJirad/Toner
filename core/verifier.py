"""Verifier role: the only component allowed to authorise DONE.

Verification is deterministic and independent of the model. It requires that
executed checks passed, the reviewer did not raise a blocking issue, and every
machine-checkable acceptance criterion can be evaluated from real state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from policy.security import SecurityError, is_secret

PASS = "PASS"
FAIL = "FAIL"
UNVERIFIED = "UNVERIFIED"


@dataclass
class CriterionOutcome:
    criterion: object
    kind: str
    status: str
    detail: str = ""

    def to_dict(self):
        return {
            "criterion": self.criterion,
            "kind": self.kind,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class VerificationResult:
    decision: str
    reasons: list = field(default_factory=list)
    criteria: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "decision": self.decision,
            "reasons": self.reasons,
            "criteria": [item.to_dict() for item in self.criteria],
            "warnings": self.warnings,
            "checks": self.checks,
        }

    def summary(self):
        if self.decision == "pass":
            return "Verification passed: executed checks and acceptance criteria agree."

        return "; ".join(self.reasons) or "Verification did not pass"


class Verifier:
    def __init__(self, settings, registry, bus=None):
        self.settings = settings
        self.registry = registry
        self.bus = bus

    # -- individual criteria --------------------------------------------

    def _command_outcome(self, check, qa):
        command = str(check.get("command", "")).strip()
        match = next(
            (item for item in qa.results if item.command == command),
            None,
        )

        if match is None:
            return UNVERIFIED, f"No QA result exists for: {command}"

        if match.status == "skipped":
            return UNVERIFIED, f"Check was skipped: {match.reason or 'no reason'}"

        if match.status == "passed":
            return PASS, f"Exit code 0: {command}"

        return FAIL, f"Exit code {match.exit_code}: {command}"

    def _file_outcome(self, check):
        kind = check.get("type")
        path = str(check.get("path", ""))

        try:
            target = self.registry.policy.resolve(path)
        except SecurityError as exc:
            return FAIL, f"Cannot evaluate {path}: {exc.code}"

        if kind == "file_exists":
            return (PASS, f"Exists: {path}") if target.is_file() else (
                FAIL,
                f"Missing file: {path}",
            )

        if kind == "file_absent":
            return (PASS, f"Absent: {path}") if not target.exists() else (
                FAIL,
                f"File should not exist: {path}",
            )

        if not target.is_file():
            return FAIL, f"Missing file: {path}"

        if target.stat().st_size > self.registry.policy.max_file_bytes:
            return UNVERIFIED, f"File too large to inspect: {path}"

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return UNVERIFIED, f"Cannot read {path}: {exc}"

        needle = str(check.get("text", ""))

        if needle and needle in content:
            return PASS, f"Found expected text in {path}"

        return FAIL, f"Expected text not found in {path}"

    def _evaluate_check(self, check, qa):
        if not isinstance(check, dict):
            return UNVERIFIED, "Check is not an object", "unknown"

        kind = str(check.get("type", "unknown"))

        if kind == "command":
            status, detail = self._command_outcome(check, qa)
        elif kind in ("file_exists", "file_absent", "text_contains"):
            status, detail = self._file_outcome(check)
        else:
            status, detail = UNVERIFIED, f"Unknown check type: {kind}"

        return status, detail, kind

    # -- protected paths -------------------------------------------------

    def _protected_reason(self, path):
        candidate = Path(path)

        if is_secret(candidate):
            return "changed a secret-named file"

        if candidate.parts and candidate.parts[0] == ".git":
            return "changed Git internals"

        try:
            state_relative = self.registry.state_root.relative_to(self.registry.root)
        except ValueError:
            return ""

        prefix = state_relative.parts

        if prefix and candidate.parts[: len(prefix)] == prefix:
            return "modified Toner state during a task"

        return ""

    # -- verification ----------------------------------------------------

    def verify(self, task, build, review, qa, changed_files, diff_text=""):
        reasons = []
        warnings = []
        criteria = []
        failed = False
        unverified = False

        # Gate 1: the build itself must have completed normally.
        if build.status != "implemented":
            failed = True
            reasons.append(
                f"Build did not complete: {build.status}"
                + (f" ({build.error})" if build.error else "")
            )

        # Gate 2: no policy violations were attempted during the build.
        if build.policy_violations:
            failed = True
            codes = sorted({item.get("code", "policy") for item in build.policy_violations})
            reasons.append(f"Policy violation during build: {', '.join(codes)}")

        # Gate 3: the reviewer must not object.
        if review.decision != "approve":
            failed = True
            reasons.append(
                f"Reviewer decision: {review.decision} "
                f"(severity {review.severity})"
            )

        if review.severity in ("high", "critical"):
            failed = True
            reasons.append(f"Reviewer severity is {review.severity}")

        if review.issues:
            for issue in review.issues[:5]:
                warnings.append(f"Review issue: {issue}")

        # Gate 4: executed tests must have passed.
        if qa.golden_failed:
            failed = True
            reasons.append("A golden check failed; DONE is forbidden")

        if not qa.passed:
            failed = True
            reasons.append(qa.summary())

        if qa.executed == 0:
            unverified = True
            reasons.append("No check command was actually executed")

        for item in qa.results:
            if item.status == "skipped":
                warnings.append(f"Skipped check: {item.command} ({item.reason})")

        # Gate 5: machine-checkable acceptance criteria.
        for check in task.checks:
            status, detail, kind = self._evaluate_check(check, qa)
            criteria.append(
                CriterionOutcome(
                    criterion=check,
                    kind=kind,
                    status=status,
                    detail=detail,
                )
            )

            if status == FAIL:
                failed = True
                reasons.append(detail)
            elif status == UNVERIFIED:
                unverified = True
                reasons.append(detail)

        if not task.checks:
            if task.require_checks:
                unverified = True
                reasons.append(
                    "Task has no machine-checkable acceptance criteria"
                )
            else:
                warnings.append(
                    "Task was imported without machine checks; verification "
                    "relies on executed QA and review only"
                )

        # Gate 6: tasks may not touch protected files or Toner's own state.
        for path in changed_files:
            reason = self._protected_reason(path)

            if reason:
                failed = True
                reasons.append(f"{path}: {reason}")

        # Non-blocking observations.
        if build.pre_existing_changes:
            warnings.append(
                "Uncommitted changes existed before this task: "
                + ", ".join(build.pre_existing_changes[:10])
            )

        if not changed_files and build.status == "implemented":
            warnings.append("No file changes were detected after the build")

        if failed:
            decision = "fail"
        elif unverified:
            decision = "insufficient"
        else:
            decision = "pass"
            reasons.append(
                "All gates passed: executed checks, review and acceptance criteria"
            )

        result = VerificationResult(
            decision=decision,
            reasons=reasons,
            criteria=criteria,
            warnings=warnings,
            checks={
                "review_decision": review.decision,
                "review_severity": review.severity,
                "qa_passed": qa.passed,
                "qa_executed": qa.executed,
                "changed_files": changed_files,
                "diff_present": bool((diff_text or "").strip()),
                "policy_violations": build.policy_violations,
            },
        )

        if self.bus is not None:
            self.bus.emit(
                "verification.passed" if decision == "pass" else "verification.failed",
                task_id=task.id,
                decision=decision,
                reasons=len(reasons),
            )

        return result
