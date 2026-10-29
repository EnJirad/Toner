"""Central orchestrator: plan, queue, run, verify, checkpoint, report.

The orchestrator owns workflow state. It never treats a model's opinion as
proof: only the Verifier can authorise DONE, and only from executed evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from core.builder import Builder
from core.context_manager import ContextManager
from core.interrupts import stop_requested
from core.model_router import ModelRouter
from core.planner import Planner
from core.qa import QA
from core.reviewer import Reviewer
from core.task_runner import TaskRunner
from core.textutil import timestamp_slug, utc_now
from core.verifier import Verifier
from events.event_bus import EventBus
from state.checkpoint import CheckpointManager
from state.evidence import EvidenceStore
from state.failure_manager import FailureManager
from state.run_store import RunStore
from state.task_store import ACTIVE_STATES, SUCCESS_STATES, Task, TaskStatus, TaskStore
from tools.registry import ToolRegistry


class Orchestrator:
    def __init__(self, settings, registry=None, router=None, bus=None):
        self.settings = settings
        self.registry = registry or ToolRegistry(settings)
        self.policy = self.registry.policy
        self.root = self.registry.root
        self.state_root = self.registry.state_root
        self.recheck = self.registry.recheck_root

        self.bus = bus or EventBus(
            self.state_root / "state" / "events.jsonl"
        )
        self.router = router or ModelRouter(settings)

        self.store = TaskStore(self.state_root / "tasks", self.bus)
        self.runs = RunStore(
            self.state_root / "runs",
            self.state_root / "state",
            self.bus,
        )
        self.evidence = EvidenceStore(
            self.root,
            self.state_root / "evidence",
            self.policy.redactor,
            self.bus,
        )
        self.checkpoints = CheckpointManager(
            self.state_root / "checkpoints",
            self.registry.git,
            self.bus,
        )
        self.failures = FailureManager(
            self.state_root / "failures",
            self.bus,
            max_attempts=settings.get("max_attempts", 3),
            max_identical=settings.get("max_identical_failures", 2),
            backoff_seconds=settings.get("retry_backoff_seconds", 2),
            backoff_max_seconds=settings.get("retry_backoff_max_seconds", 30),
            redactor=self.policy.redactor,
        )

        self.planner = Planner(settings, self.router, self.bus)
        self.context = ContextManager(
            max_chars=settings.get("max_context_chars", 24000),
            router=self.router,
            bus=self.bus,
            redactor=self.policy.redactor,
        )
        self.builder = Builder(
            settings,
            self.router,
            self.registry,
            self.bus,
            self.context,
        )
        self.reviewer = Reviewer(
            settings, self.router, self.bus, self.policy.redactor
        )
        self.qa = QA(settings, self.registry, self.bus)
        self.verifier = Verifier(settings, self.registry, self.bus)

        self.legacy_error = ""
        self._import_legacy()

    # -- state -----------------------------------------------------------

    def _import_legacy(self):
        legacy = self.recheck / "auto_state.json"

        if not legacy.is_file():
            return

        try:
            count = self.store.import_legacy(legacy)
        except RuntimeError as exc:
            self.legacy_error = str(exc)
            return

        if count:
            print(f"Imported {count} task(s) from the legacy auto_state.json ledger.")

    def _require_tasks(self):
        if self.legacy_error:
            raise RuntimeError(self.legacy_error)

        tasks = self.store.all()

        if not tasks:
            print("No saved plan. Use /auto to create one from .toner/files_md/.")

        return tasks

    def _runner(self, run):
        return TaskRunner(
            self.settings,
            self.store,
            self.bus,
            self.builder,
            self.reviewer,
            self.qa,
            self.verifier,
            self.failures,
            self.checkpoints,
            self.evidence,
            self.registry.git,
            self.registry,
            run,
        )

    # -- planning --------------------------------------------------------

    def auto(self):
        plans_dir = self.state_root / "files_md"
        files = self.planner.read_plan_files(plans_dir)

        if not files:
            print(f"No Markdown plan files found in {plans_dir}")
            print("Add .md plan files there before using /auto.")
            return

        unfinished = self.store.unfinished()

        if unfinished:
            if not self.registry.ask(
                f"{len(unfinished)} unfinished task(s) exist. Replace them with a "
                "new plan? Existing records are archived, not deleted.",
                always=True,
            ):
                print("Cancelled. The existing plan is unchanged.")
                return

        print(f"Found {len(files)} plan file(s):")

        for path in files:
            print(" -", path.relative_to(self.state_root))

        if not self.registry.ask(
            "Analyse these plans and create a task list?", always=True
        ):
            print("Cancelled.")
            return

        plan_text = self.planner.read_plan_text(files)
        fingerprint = self.planner.fingerprint(files, self.state_root)

        try:
            specs = self.planner.plan(plan_text)
        except Exception as exc:
            self.bus.emit("agent.error", phase="planning", error=str(exc))
            print(f"Planning failed: {exc}")
            return

        label = timestamp_slug()
        task_archive = self.store.archive(label)
        evidence_archive = self.evidence.archive(label)

        if task_archive:
            print(f"Archived previous tasks to {task_archive}")

        if evidence_archive:
            print(f"Archived previous evidence to {evidence_archive}")

        tasks = self.store.add_tasks(specs)
        self.runs.update_session(
            status="planned",
            plan_fingerprint=fingerprint,
            plan_files=[str(path.relative_to(self.state_root)) for path in files],
            reason="",
        )
        self.checkpoints.create(
            "plan-created",
            run_id="",
            phase="PLANNING",
            extra={"plan_fingerprint": fingerprint},
        )

        self._print_plan(tasks)

        if not self.registry.ask("Start executing this plan?", always=True):
            self.runs.update_session(status="paused", reason="plan confirmed but not started")
            print("Plan saved. Use /resume when ready.")
            return

        self.execute(tasks, kind="auto")

    # -- execution -------------------------------------------------------

    def _resolve_dead_dependencies(self):
        blocked = []

        for task in self.store.all():
            if task.status != TaskStatus.QUEUED.value:
                continue

            for dependency in task.dependencies:
                parent = self.store.get(dependency)
                dead = parent is None or parent.status in (
                    TaskStatus.BLOCKED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                )

                if dead:
                    task.transition(TaskStatus.BLOCKED)
                    task.last_error = (
                        f"Dependency {dependency} is missing or blocked"
                    )
                    task.notes.append(task.last_error)
                    self.store.save(task, event="task.blocked")
                    self.bus.emit(
                        "task.blocked",
                        task_id=task.id,
                        reason=task.last_error,
                    )
                    blocked.append(task.id)
                    break

        return blocked

    def execute(self, tasks, kind="auto"):
        limit = max(1, int(self.settings.get("max_auto_steps_per_session", 10)))
        run = self.runs.start(
            [task.id for task in tasks],
            kind=kind,
            session_limit=limit,
        )
        runner = self._runner(run)
        batch_limits = self.settings.get("batch_limits", {})
        used_by_risk = {}
        self.bus.emit(
            "agent.resumed" if kind == "resume" else "agent.started",
            run_id=run["id"],
            tasks=len(tasks),
        )

        outcome = "paused"

        try:
            while run["steps"] < limit:
                if stop_requested():
                    outcome = "paused"
                    break

                self._resolve_dead_dependencies()
                task = self.store.next_task()

                if task is None:
                    stuck = [
                        item
                        for item in self.store.all()
                        if item.status == TaskStatus.QUEUED.value
                    ]

                    for item in stuck:
                        item.transition(TaskStatus.BLOCKED)
                        item.last_error = (
                            "Dependency cycle or unsatisfiable dependency; "
                            "manual review required"
                        )
                        self.store.save(item, event="task.blocked")

                    break

                risk_limit = int(batch_limits.get(task.risk, 1))

                if used_by_risk.get(task.risk, 0) >= risk_limit:
                    print(
                        f"Batch limit for {task.risk}-risk work reached "
                        f"({risk_limit}); pausing before {task.id}."
                    )
                    outcome = "paused"
                    break

                used_by_risk[task.risk] = used_by_risk.get(task.risk, 0) + 1
                status = runner.run_task(task)
                run["steps"] = int(run.get("steps", 0)) + 1
                self.runs.save(run)

                if status == TaskStatus.BLOCKED.value:
                    dependents = self.store.block_dependents(
                        task.id,
                        f"Dependency {task.id} is blocked",
                    )

                    if dependents:
                        print(f"Blocked dependent tasks: {', '.join(dependents)}")

                if status == TaskStatus.PAUSED.value:
                    outcome = "paused"
                    break
        except BaseException:
            self.runs.finish(run, "failed", note="Unexpected orchestrator error")
            raise
        else:
            outcome = self._outcome(stop_requested())
            self.runs.finish(run, outcome)
        finally:
            self.bus.emit(
                "agent.finished" if outcome == "completed" else "agent.paused",
                run_id=run["id"],
                outcome=outcome,
                steps=run["steps"],
            )

        self._print_outcome(outcome)
        return outcome

    def _outcome(self, stopped=False):
        tasks = self.store.all()

        if not tasks:
            return "completed"

        if stopped:
            return "paused"

        if all(task.status in SUCCESS_STATES for task in tasks):
            return "completed"

        if any(task.status == TaskStatus.BLOCKED.value for task in tasks):
            return "blocked"

        if any(task.status in ACTIVE_STATES for task in tasks):
            return "paused"

        if any(task.status == TaskStatus.QUEUED.value for task in tasks):
            return "paused"

        return "blocked"

    # -- resume ----------------------------------------------------------

    def resume(self):
        tasks = self._require_tasks()

        if not tasks:
            return

        session = self.runs.session()
        stored_fingerprint = session.get("plan_fingerprint", "")

        if stored_fingerprint:
            files = self.planner.read_plan_files(self.state_root / "files_md")
            current = self.planner.fingerprint(files, self.state_root)

            if current != stored_fingerprint:
                print(
                    "⚠️ The Markdown plan files changed after this task list was "
                    "created, so Toner will not continue automatically."
                )

                if self.registry.ask(
                    "Create a new plan from the current Markdown (replaces the old one)?",
                    always=True,
                ):
                    self.auto()

                return

        active = self.runs.active()

        if active and active.get("status") == "running":
            self.runs.finish(
                active,
                "abandoned",
                note="Previous session ended without a clean shutdown",
            )
            print(
                f"Previous run {active.get('id', '')} was not closed cleanly; "
                "recorded as abandoned."
            )

        self._print_resume_report(tasks)

        if not self.registry.ask("Continue from the saved checkpoint?", always=True):
            return

        for task in tasks:
            if task.status in ACTIVE_STATES:
                task.transition(TaskStatus.PAUSED)
                self.store.save(task)

            if task.status in (
                TaskStatus.PAUSED.value,
                TaskStatus.FAILED.value,
                TaskStatus.RETRYING.value,
            ):
                task.transition(TaskStatus.QUEUED)
                task.current_step = "QUEUED"
                self.store.save(task)

        self.bus.emit("agent.resumed", run_id="", tasks=len(tasks))
        self.execute(tasks, kind="resume")

    def _print_resume_report(self, tasks):
        session = self.runs.session()
        current = next(
            (task for task in tasks if task.status in ACTIVE_STATES),
            None,
        )
        current = current or next(
            (
                task
                for task in tasks
                if task.status
                in (TaskStatus.QUEUED.value, TaskStatus.PAUSED.value, TaskStatus.BLOCKED.value)
            ),
            None,
        )
        fingerprints = [
            entry.get("signature", "")
            for task in tasks
            for entry in task.failure_history[-1:]
        ]

        print("\n=== RESUME STATE ===")
        print("Task ID:", current.id if current else "-")
        print("Status:", current.status if current else session.get("status", "idle"))
        print("Phase:", (current.current_step or current.status) if current else "-")
        print("Attempts:", current.attempts if current else 0)
        print("Last Error:", (current.last_error or "-")[:300] if current else "-")
        print(
            "Failure Fingerprint:",
            ", ".join(item for item in fingerprints if item) or "-",
        )
        print(
            "Last Known Good Commit:",
            (current.last_known_good_commit or "-")[:12] if current else "-",
        )
        print("Current Commit:", (current.current_commit or "-")[:12] if current else "-")
        last_test = (current.last_test or {}) if current else {}
        print(
            "Last Successful Test:",
            f"{last_test.get('passed', 0)} passed / {last_test.get('failed', 0)} failed",
        )
        print("Next Action:", self._next_action(tasks))

    # -- status ----------------------------------------------------------

    def status(self):
        print("\nTONER STATUS")

        if self.legacy_error:
            print("⚠️ Legacy state could not be imported:", self.legacy_error)

        try:
            tasks = self.store.all()
        except RuntimeError as exc:
            print("Project:", self.root)
            print("State: corrupt task ledger")
            print("Error:", exc)
            return

        counts = self.store.counts()
        session = self.runs.session()
        active = self.runs.active()
        latest = self.runs.latest()
        run = active or latest
        queued = [task for task in tasks if task.status == TaskStatus.QUEUED.value]
        blocked_tasks = [
            task for task in tasks if task.status == TaskStatus.BLOCKED.value
        ]
        current = (
            next((task for task in tasks if task.status in ACTIVE_STATES), None)
            or (queued[0] if queued else None)
            or (blocked_tasks[0] if blocked_tasks else None)
            or (tasks[-1] if tasks else None)
        )

        print("Project:", self.root)
        print(
            "Current Run:",
            f"{run.get('id')} ({run.get('status')})" if run else "-",
        )
        print(
            "Current Task:",
            f"{current.id} — {current.title}" if current else "-",
        )
        phase = (current.current_step or current.status) if current else session.get("status", "idle")
        print("Phase:", phase)
        print("Status:", session.get("status", "idle"))
        print("\nTasks:")

        for label in ("queued", "running", "completed", "failed", "blocked"):
            print(f"  {label}: {counts.get(label, 0)}")

        print("\nAttempts:", current.attempts if current else 0)
        print("Last Error:", ((current.last_error if current else "") or "-")[:300])
        fingerprints = [
            entry.get("signature", "")
            for entry in (current.failure_history[-1:] if current else [])
            if entry.get("signature")
        ]
        print("Failure Fingerprint:", ", ".join(fingerprints) or "-")
        print(
            "Last Known Good Commit:",
            ((current.last_known_good_commit if current else "") or "-")[:12],
        )
        print(
            "Current Commit:",
            ((current.current_commit if current else "") or "-")[:12],
        )

        passed = sum(int((task.last_test or {}).get("passed", 0)) for task in tasks)
        failed = sum(int((task.last_test or {}).get("failed", 0)) for task in tasks)
        print("\nTests:")
        print("  passed:", passed)
        print("  failed:", failed)
        print("\nNext Action:", self._next_action(tasks))

        if tasks:
            print("\nTask List:")

            for task in tasks:
                print(
                    f"- {task.id} [{task.status}] risk={task.risk} "
                    f"attempts={task.attempts} {task.title}"
                )

    def _next_action(self, tasks):
        blocked = [task.id for task in tasks if task.status == TaskStatus.BLOCKED.value]
        active = next((task for task in tasks if task.status in ACTIVE_STATES), None)

        if active:
            return f"Continue {active.id} at {active.current_step or active.status} (/resume)"

        if blocked:
            return "Review blocked tasks manually: " + ", ".join(blocked[:5])

        queued = [task for task in tasks if task.status == TaskStatus.QUEUED.value]

        if queued:
            return f"Run /resume to continue {len(queued)} queued task(s)"

        if tasks and all(task.status in SUCCESS_STATES for task in tasks):
            return "Nothing to do: all tasks are complete"

        return "Use /auto to create a plan, or /check for an ad-hoc command"

    # -- report ----------------------------------------------------------

    def report(self):
        stamp = timestamp_slug()
        path = self.recheck / "reports" / f"report-{stamp}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        tasks = self.store.all()
        session = self.runs.session()
        run = self.runs.active() or self.runs.latest()
        counts = self.store.counts()
        lines = [
            "# Toner Engineering Report",
            "",
            f"- Generated: {utc_now()}",
            f"- Project: `{self.root}`",
            f"- Run: `{run.get('id') if run else '-'}` "
            f"({run.get('status') if run else 'none'})",
            f"- Session status: **{session.get('status', 'idle')}**",
            "",
            "## Task summary",
            "",
            f"- queued: {counts.get('queued', 0)}",
            f"- running: {counts.get('running', 0)}",
            f"- completed: {counts.get('completed', 0)}",
            f"- failed: {counts.get('failed', 0)}",
            f"- blocked: {counts.get('blocked', 0)}",
            "",
        ]

        for task in tasks:
            tests = task.last_test or {}
            review = task.review or {}
            verification = task.verification_result or {}
            lines.extend(
                [
                    f"## {task.id} — {task.title}",
                    "",
                    f"- Status: **{task.status}**",
                    f"- Risk: {task.risk}",
                    f"- Attempts: {task.attempts}",
                    f"- Dependencies: {', '.join(task.dependencies) or 'none'}",
                    f"- Current commit: `{task.current_commit or '-'}`",
                    f"- Last known good commit: `{task.last_known_good_commit or '-'}`",
                    "",
                    "### Acceptance criteria",
                    "",
                ]
            )

            if task.acceptance_criteria:
                lines.extend(f"- {item}" for item in task.acceptance_criteria)
            else:
                lines.append("- (none recorded)")

            lines.extend(
                [
                    "",
                    "### Tests",
                    "",
                    f"- Executed: {tests.get('executed', 0)}",
                    f"- Passed: {tests.get('passed', 0)}",
                    f"- Failed: {tests.get('failed', 0)}",
                ]
            )

            for command in tests.get("commands", []) or []:
                lines.append(f"- `{command}`")

            lines.extend(
                [
                    "",
                    "### Review",
                    "",
                    f"- Decision: {review.get('decision', 'not recorded')}",
                    f"- Severity: {review.get('severity', 'not recorded')}",
                ]
            )

            for issue in review.get("issues", []) or []:
                lines.append(f"- Issue: {issue}")

            lines.extend(
                [
                    "",
                    "### Verification",
                    "",
                    f"- Decision: {verification.get('decision', 'not recorded')}",
                ]
            )

            for reason in verification.get("reasons", []) or []:
                lines.append(f"- {reason}")

            for warning in verification.get("warnings", []) or []:
                lines.append(f"- Warning: {warning}")

            if task.failure_history:
                lines.extend(["", "### Failures", ""])

                for failure in task.failure_history[-5:]:
                    lines.append(
                        f"- {failure.get('phase', '')}: "
                        f"{failure.get('type', '')} "
                        f"`{failure.get('signature', '')}` — "
                        f"{failure.get('message', '')}"
                    )

            if task.notes:
                lines.extend(["", "### Notes", ""])
                lines.extend(f"- {note}" for note in task.notes)

            lines.extend(
                [
                    "",
                    "### Evidence",
                    "",
                ]
            )

            if task.evidence:
                lines.extend(f"- `{item}`" for item in sorted(set(task.evidence)))
            else:
                lines.append("- (none recorded)")

            lines.append("")

        lines.extend(["## Next actions", "", f"- {self._next_action(tasks)}", ""])

        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Report written: {path}")
        return path

    # -- ad-hoc ----------------------------------------------------------

    def check(self, prompt=None):
        if prompt is None:
            path = self.state_root / "new_command.md"

            if not path.exists():
                path.write_text(
                    "# New Command\n\nWrite the task here.\n",
                    encoding="utf-8",
                )

            content = path.read_text(encoding="utf-8").strip()

            if not content or content == "# New Command":
                print(f"Write a command in: {path}")
                return

            print("\nCommand read:")
            print("-" * 50)
            print(content[:6000])
            print("-" * 50)

            if not self.registry.ask(
                "Start working on new_command.md?", always=True
            ):
                print("Cancelled.")
                return
        else:
            content = str(prompt).strip()

            if not content:
                return

        # Ad-hoc checks must not overwrite the saved plan's session status.
        run = self.runs.start(
            [], kind="check", session_limit=1, update_session=False
        )
        self.checkpoints.create("before-check", run_id=run["id"], phase="BUILDING")
        task = Task(
            id=f"CHECK-{run['id'].split('-')[-1]}",
            title="Ad-hoc command",
            description=content,
            checks=[],
            require_checks=False,
            risk="high",
        )

        try:
            build = self.builder.build(task, run["id"])
        except Exception as exc:
            self.runs.finish(run, "failed", note=str(exc))
            self.bus.emit("agent.error", phase="check", error=str(exc))
            print(f"Toner error: {exc}")
            return

        self.evidence.write(task.id, "build", asdict(build))
        self.evidence.write(
            task.id,
            "diff",
            {
                "changed_files": build.files_changed,
                "git_status": self.registry.git.status(),
            },
        )
        self.checkpoints.create("after-check", run_id=run["id"], phase="BUILDING")
        self.runs.finish(
            run,
            "completed" if build.status == "implemented" else "failed",
            note=f"ad-hoc build: {build.status} (no verification)",
            update_session=False,
        )

        print("\nTONER >\n" + (build.summary or "(no summary)"))
        print(
            "\nNote: /check runs an ad-hoc build without independent "
            "verification. Use /auto for evidence-gated tasks."
        )
        print(
            f"\nChanged files: {', '.join(build.files_changed) or 'none'}"
        )
        print(
            "Checks executed: "
            + (
                ", ".join(
                    f"{item.get('command')} (exit {item.get('returncode')})"
                    for item in build.checks_run
                )
                or "none"
            )
        )

    # -- shutdown --------------------------------------------------------

    def shutdown(self):
        session = self.runs.session()
        active = self.runs.active()

        if active and active.get("status") == "running":
            self.runs.finish(
                active,
                "paused",
                note="Toner exited cleanly before completion",
            )

        if session.get("status") == "running":
            self.runs.update_session(status="paused", reason="Toner exited")
            self.checkpoints.create(
                "shutdown",
                run_id=session.get("run_id", ""),
                phase="SHUTDOWN",
            )
            self.bus.emit("agent.paused", reason="shutdown")

    # -- output ----------------------------------------------------------

    def _print_plan(self, tasks):
        print("\nPlan created:")

        for task in tasks:
            print(
                f"{task.id} [{task.status}] risk={task.risk} {task.title}"
            )

            for criterion in task.acceptance_criteria:
                print(f"    criterion: {criterion}")

            for check in task.checks:
                print(f"    check: {json.dumps(check, ensure_ascii=False)}")

    def _print_outcome(self, outcome):
        tasks = self.store.all()
        counts = self.store.counts()
        print(
            f"\nRun finished: {outcome} — "
            f"{counts.get('completed', 0)} completed, "
            f"{counts.get('blocked', 0)} blocked, "
            f"{counts.get('queued', 0)} queued."
        )

        if outcome == "paused":
            print("Use /resume to continue from the saved checkpoint.")

        if outcome == "blocked":
            blocked = [
                task.id
                for task in tasks
                if task.status == TaskStatus.BLOCKED.value
            ]
            print("Blocked tasks: " + ", ".join(blocked[:10]))
            print("Evidence and failure fingerprints are saved under .toner/.")
