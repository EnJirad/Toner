"""Task runner: one task through build, review, test, verify and commit."""

from __future__ import annotations

import time
from dataclasses import asdict

from core.interrupts import stop_requested
from state.task_store import ACTIVE_STATES, TaskStatus


class TaskRunner:
    def __init__(
        self,
        settings,
        store,
        bus,
        builder,
        reviewer,
        qa,
        verifier,
        failures,
        checkpoints,
        evidence,
        git,
        registry,
        run=None,
    ):
        self.settings = settings
        self.store = store
        self.bus = bus
        self.builder = builder
        self.reviewer = reviewer
        self.qa = qa
        self.verifier = verifier
        self.failures = failures
        self.checkpoints = checkpoints
        self.evidence = evidence
        self.git = git
        self.registry = registry
        self.run = run or {}

    # -- helpers ---------------------------------------------------------

    def _run_id(self):
        return self.run.get("id", "")

    def _save(self, task):
        self.store.save(task)

    def _record(self, task, name, payload):
        attempt = int(task.attempts or 0)
        self.evidence.write(task.id, f"attempt-{attempt:02d}-{name}", payload)
        relative = self.evidence.write(task.id, name, payload)

        if relative not in task.evidence:
            task.evidence.append(relative)

        return relative

    def _diff_text(self, changed_files):
        diff = self.git.diff()
        text = diff.get("output", "") if diff.get("returncode") == 0 else ""
        staged = self.git.diff_staged()

        if staged.get("returncode") == 0 and staged.get("output"):
            text += "\n" + staged["output"]

        untracked_sections = []

        for path in changed_files:
            if path in text:
                continue

            try:
                target = self.registry.policy.resolve(path)
            except Exception:
                continue

            if not target.is_file():
                continue

            try:
                if target.stat().st_size > self.registry.policy.max_file_bytes:
                    continue
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            untracked_sections.append(
                f"\n--- new or untracked file: {path} ---\n{content[:4000]}"
            )

        return (text + "".join(untracked_sections))[:24000]

    def _pause(self, task):
        if task.status in ACTIVE_STATES:
            try:
                task.transition(TaskStatus.PAUSED)
            except Exception:
                pass

        task.current_step = "PAUSED"
        self._save(task)
        self.checkpoints.create("paused", task, self._run_id(), "PAUSED")
        self.bus.emit("agent.paused", task_id=task.id, reason="user request")
        return task.status

    def _sleep(self, seconds):
        deadline = time.monotonic() + max(0.0, float(seconds))

        while time.monotonic() < deadline:
            if stop_requested():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _fail(self, task, failure, phase):
        decision = self.failures.record(task, failure)
        self._record(
            task,
            "failure",
            {
                "phase": phase,
                "decision": asdict(decision),
                "failure": failure.to_dict(),
            },
        )

        if decision.action != "retry":
            self._save(task)
            task.transition(TaskStatus.BLOCKED)
            task.current_step = "BLOCKED"
            task.notes.append(decision.reason)
            task.last_error = failure.message[:4000]
            self._save(task)
            self.checkpoints.create("blocked", task, self._run_id(), "BLOCKED")
            self.bus.emit(
                "task.blocked",
                task_id=task.id,
                reason=decision.reason,
                classification=decision.classification,
                signature=decision.signature,
            )
            return False

        task.transition(TaskStatus.RETRYING)
        task.current_step = "RETRYING"
        self._save(task)
        self.bus.emit(
            "task.retrying",
            task_id=task.id,
            attempt=task.attempts,
            reason=decision.reason,
            delay=decision.delay,
        )
        self._sleep(decision.delay)
        return True

    # -- pipeline --------------------------------------------------------

    def run_task(self, task):
        while True:
            if stop_requested():
                return self._pause(task)

            task.attempts = int(task.attempts or 0) + 1
            task.current_step = "BUILDING"
            task.transition(TaskStatus.BUILDING)
            self._save(task)
            self.checkpoints.create("before-build", task, self._run_id(), "BUILDING")
            self.bus.emit(
                "task.started",
                task_id=task.id,
                attempt=task.attempts,
                run_id=self._run_id(),
            )

            try:
                build = self.builder.build(task, self._run_id())
            except Exception as exc:
                failure = self.failures.from_exception(exc, "BUILDING")

                if not self._fail(task, failure, "BUILDING"):
                    return task.status

                continue

            self._record(task, "build", asdict(build))
            self._save(task)
            self.checkpoints.create("after-build", task, self._run_id(), "BUILDING")

            if build.status == "stopped":
                return self._pause(task)

            if build.status != "implemented":
                failure = self.failures.from_text(
                    build.error or f"Build status: {build.status}",
                    phase="BUILDING",
                    kind="build",
                )

                if not self._fail(task, failure, "BUILDING"):
                    return task.status

                continue

            diff_text = self._diff_text(build.files_changed)
            self._record(
                task,
                "diff",
                {
                    "changed_files": build.files_changed,
                    "pre_existing_changes": build.pre_existing_changes,
                    "diff": diff_text,
                    "git_status": self.git.status(),
                },
            )

            # -- review --------------------------------------------------
            task.transition(TaskStatus.REVIEWING)
            task.current_step = "REVIEWING"
            self._save(task)

            try:
                review = self.reviewer.review(
                    task, build, diff_text, build.files_changed
                )
            except Exception as exc:
                failure = self.failures.from_exception(exc, "REVIEWING")

                if not self._fail(task, failure, "REVIEWING"):
                    return task.status

                continue

            task.review = review.to_dict()
            self._record(task, "review", review.to_dict())
            self._save(task)

            # -- QA ------------------------------------------------------
            task.transition(TaskStatus.TESTING)
            task.current_step = "TESTING"
            self._save(task)
            qa_result = self.qa.run(task, build.files_changed)
            task.last_test = {
                "passed": len(
                    [item for item in qa_result.results if item.status == "passed"]
                ),
                "failed": len(
                    [item for item in qa_result.results if item.status == "failed"]
                ),
                "skipped": len(
                    [
                        item
                        for item in qa_result.results
                        if item.status == "skipped"
                    ]
                ),
                "executed": qa_result.executed,
                "commands": qa_result.commands,
            }
            self._record(task, "tests", qa_result.to_dict())
            self._save(task)

            # -- verify --------------------------------------------------
            task.transition(TaskStatus.VERIFYING)
            task.current_step = "VERIFYING"
            self._save(task)
            verification = self.verifier.verify(
                task,
                build,
                review,
                qa_result,
                build.files_changed,
                diff_text,
            )
            task.verification_result = verification.to_dict()
            self._record(task, "verification", verification.to_dict())

            if verification.decision != "pass":
                message = verification.summary()

                if review.severity == "critical":
                    message = "Security violation: critical review finding. " + message

                failure = self.failures.from_text(
                    message,
                    phase="VERIFYING",
                    kind="verification",
                )
                self._save(task)

                if not self._fail(task, failure, "VERIFYING"):
                    return task.status

                continue

            # -- commit (only after verification) ------------------------
            sha = self.git.current_sha()

            if sha.get("returncode") == 0:
                task.current_commit = sha.get("sha", "")

            git_ready = self.git.status().get("returncode") == 0
            wants_commit = bool(self.settings.get("auto_commit", False)) and bool(
                build.files_changed
            )

            if wants_commit and not git_ready:
                task.notes.append(
                    "auto_commit skipped: this project is not a Git repository"
                )

            if wants_commit and git_ready:
                task.transition(TaskStatus.COMMITTING)
                task.current_step = "COMMITTING"
                self._save(task)
                commit = self.git.commit(
                    f"toner: {task.id} {task.title}".strip(),
                    files=build.files_changed,
                )

                if commit.get("returncode") != 0:
                    failure = self.failures.from_text(
                        commit.get("error") or "git commit failed",
                        phase="COMMITTING",
                        kind="git",
                        exit_code=commit.get("returncode"),
                    )
                    self._record(task, "commit", commit)

                    if not self._fail(task, failure, "COMMITTING"):
                        return task.status

                    continue

                task.current_commit = commit.get("sha", task.current_commit)
                task.last_known_good_commit = task.current_commit
                self.bus.emit(
                    "commit.created",
                    task_id=task.id,
                    sha=task.current_commit,
                )
                self._record(task, "commit", commit)
            elif self.settings.get("auto_commit", False) and not build.files_changed:
                task.notes.append(
                    "auto_commit is enabled but the task produced no file changes"
                )

            task.transition(TaskStatus.DONE)
            task.current_step = "DONE"
            task.result = build.summary[:8000]
            self._save(task)
            self.checkpoints.create("done", task, self._run_id(), "DONE")
            self.bus.emit(
                "task.completed",
                task_id=task.id,
                attempts=task.attempts,
                commit=task.current_commit,
            )
            return task.status
