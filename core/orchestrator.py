"""Central orchestrator: plan, queue, run, verify, checkpoint, report.

The orchestrator owns workflow state. It never treats a model's opinion as
proof: only the Verifier can authorise DONE, and only from executed evidence.
"""

from __future__ import annotations

import json
from dataclasses import asdict

from core.agent_loop import READ_ONLY_TOOLS, AgentLoop
from core.builder import Builder
from core.context_manager import ContextManager
from core.conversation import Conversation
from core.intent import DEFAULT_CLARIFICATION, Intent, IntentRouter
from core.interrupts import request_stop, reset_stop, stop_requested
from core.model_router import ModelRouter
from core.planner import Planner
from core.qa import QA
from core.reviewer import Reviewer
from core.task_runner import TaskRunner
from core.textutil import timestamp_slug, utc_now
from core.verifier import Verifier
from events.event_bus import EventBus
from state.baseline import ChangeTracker
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

        self.conversation = Conversation(redactor=self.policy.redactor)
        self.intent = IntentRouter(
            settings, model_router=self.router, redactor=self.policy.redactor
        )
        self.changes = ChangeTracker(self.registry)
        self.inspector = AgentLoop(
            settings,
            self.router,
            self.registry,
            bus=self.bus,
            context=self.context,
            redactor=self.policy.redactor,
        )

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

    def _resolve_dead_dependencies(self, only_ids=None):
        blocked = []

        for task in self.store.all():
            if only_ids is not None and task.id not in only_ids:
                continue

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

    def _next_task(self, only_ids=None):
        """Pick the next runnable task, optionally restricted to ``only_ids``."""
        if only_ids is None:
            return self.store.next_task()

        ready = {item.id for item in self.store.ready()}

        for task_id in only_ids:
            task = self.store.get(task_id)

            if task is not None and task.status == TaskStatus.QUEUED.value:
                if task.id in ready:
                    return task

                # Dependency outside the restricted set is still pending.
                return None

        return None

    def execute(self, tasks, kind="auto", only_ids=None):
        limit = max(1, int(self.settings.get("max_auto_steps_per_session", 10)))
        only = set(only_ids) if only_ids is not None else None
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

                self._resolve_dead_dependencies(only)
                task = self._next_task(only)

                if task is None:
                    if only is not None:
                        # Restricted run: never touch unrelated queued tasks.
                        break

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
            outcome = self._outcome(stop_requested(), only)
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

    def _outcome(self, stopped=False, only_ids=None):
        tasks = self.store.all()

        if only_ids is not None:
            tasks = [task for task in tasks if task.id in only_ids]

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
        provider = self.settings.get("provider") or {}
        print(
            "Model:",
            f"{self.router.model_for('builder')} "
            f"({provider.get('type', 'openai_compatible')})",
        )
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
                    f"- Skipped (not run): {tests.get('skipped', 0)}",
                ]
            )

            if not tests.get("executed"):
                lines.append("- ⚠️ Checks not run: **no check was executed**")

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

    # -- conversation routing -------------------------------------------

    def stop(self):
        """Safely cancel the active task and return to an idle session.

        The CLI is synchronous, so /stop is typed while idle: it cancels any
        run left open (for example after a Ctrl+C mid-operation), parks active
        tasks in PAUSED with a checkpoint, and clears the stop flag so the
        session stays usable.
        """
        request_stop()
        paused = []
        active_run = self.runs.active()

        if active_run and active_run.get("status") == "running":
            self.runs.finish(
                active_run, "paused", note="stopped by user (/stop)"
            )
            print(f"Run {active_run.get('id')} paused.")

        for task in self.store.all():
            if task.status not in ACTIVE_STATES:
                continue

            try:
                task.transition(TaskStatus.PAUSED)
            except Exception:
                continue

            task.current_step = "PAUSED"
            self.store.save(task, event="task.paused")
            self.checkpoints.create(
                "stopped", task, active_run.get("id", "") if active_run else "",
                "PAUSED",
            )
            paused.append(task.id)

        if paused:
            print(
                "Paused tasks: "
                + ", ".join(paused)
                + ". Use /resume to continue."
            )
        else:
            print("Nothing is running right now.")

        self.bus.emit("agent.stop_requested", paused=paused)
        reset_stop()

    def active_task_context(self):
        tasks = self.store.all()
        active = next(
            (item for item in tasks if item.status in ACTIVE_STATES), None
        )

        if active:
            return f"{active.id} [{active.status}] {active.title}"

        queued = [item for item in tasks if item.status == TaskStatus.QUEUED.value]

        if queued:
            return (
                f"{len(queued)} queued task(s), next: "
                f"{queued[0].id} {queued[0].title}"
            )

        return ""

    def handle(self, text):
        """Route one natural-language message to the right workflow.

        Never raises: a routing or model failure is reported to the user and
        recorded on the event bus instead of crashing the CLI.
        """
        raw = str(text or "").strip()

        if not raw:
            return None

        active = self.active_task_context()
        result = self.intent.classify(
            raw,
            active_task=active,
            history=self.conversation.prompt_context(),
        )
        self.bus.emit(
            "intent.classified",
            intent=result.intent,
            source=result.source,
            confidence=round(result.confidence, 2),
            follow_up=result.follow_up,
        )

        handlers = {
            Intent.CHAT.value: self._handle_chat,
            Intent.QUESTION.value: self._handle_inspect,
            Intent.INSPECT.value: self._handle_inspect,
            Intent.PLAN.value: self._handle_plan,
            Intent.CODE_TASK.value: self._handle_code_task,
            Intent.TEST.value: self._handle_tests,
            Intent.GIT_TASK.value: self._handle_git,
            Intent.CLARIFICATION.value: self._handle_clarification,
        }
        handler = handlers.get(result.intent, self._handle_clarification)

        try:
            return handler(raw, result)
        except Exception as exc:
            self.bus.emit("agent.error", phase=result.intent, error=str(exc))
            print(f"Toner could not complete that request: {exc}")
            return None

    # -- handlers --------------------------------------------------------

    def _finish_turn(self, user_text, reply):
        self.conversation.add("user", user_text)
        self.conversation.add("assistant", reply)
        print(self.policy.redactor.redact(str(reply)))
        return reply

    def _chat_system(self):
        return (
            "You are Toner, a helpful AI coding assistant chatting with a "
            "user in a terminal.\n"
            f"Project root: {self.root}\n"
            "Answer in the user's language (Thai or English). Be concise and "
            "conversational. In this mode you have no tools: never claim to "
            "have read files, run commands or changed anything. If the user "
            "asks to inspect, plan, test or change the project, respond "
            "naturally — Toner's router handles that automatically.\n"
            "Never reveal secrets, API keys or credentials."
        )

    def _handle_chat(self, text, result):
        messages = (
            [{"role": "system", "content": self._chat_system()}]
            + self.conversation.history()
            + [{"role": "user", "content": text}]
        )

        try:
            reply = self.router.chat("chat", messages, temperature=0.3)
        except Exception as exc:
            self.bus.emit("agent.error", phase="chat", error=str(exc))
            self.conversation.add("user", text)
            print(f"Toner could not reach the model: {exc}")
            return None

        content = str(reply.get("content") or "").strip() or "(no reply)"
        self._finish_turn(text, content)
        self.conversation.record_result(
            Intent.CHAT.value, summary=content[:200]
        )
        return content

    def _handle_inspect(self, text, result):
        """Read-only investigation: QUESTION and INSPECT share this path."""
        history = self.conversation.history()
        system = self._chat_system() + (
            "\n\nRead-only inspection mode: you may use the read-only tools "
            "to examine the project and then explain your findings. No files "
            "may be modified and no commands may be run."
        )
        rounds = max(
            1,
            min(int(self.settings.get("max_tool_rounds", 50)), 12),
        )
        loop_result = self.inspector.run(
            system=system,
            prompt=text,
            role="inspector",
            allowed_tools=READ_ONLY_TOOLS,
            max_rounds=rounds,
            history=history,
            temperature=0.1,
        )

        if loop_result.status == "failed" and loop_result.exception is not None:
            self.bus.emit("agent.error", phase="inspect", error=str(loop_result.error))
            self.conversation.add("user", text)
            print(f"Toner could not reach the model: {loop_result.error}")
            return None

        if loop_result.status == "failed":
            reply = f"Inspection failed: {loop_result.error}"
        elif loop_result.status == "stopped":
            reply = "Inspection stopped before completion."
        else:
            reply = loop_result.final_text or "(no findings)"

        refused = [
            item
            for item in loop_result.violations
            if item.get("code") == "tool_not_allowed"
        ]

        if refused:
            reply += (
                f"\n\n({len(refused)} tool call(s) outside the read-only "
                "allowlist were refused during inspection)"
            )

        self._finish_turn(text, reply)
        self.conversation.record_result(result.intent, summary=reply[:200])
        self.bus.emit(
            "inspect.completed",
            rounds=loop_result.rounds,
            tool_calls=loop_result.tool_calls,
        )
        return reply

    def _handle_plan(self, text, result):
        messages = (
            [
                {
                    "role": "system",
                    "content": (
                        "You are Toner, a planning assistant. Produce a "
                        "concise implementation plan for the user's request: "
                        "numbered steps, files likely affected, risks and how "
                        "to verify the change. You have no tools; do not claim "
                        "to have read or changed any file. Answer in the "
                        "user's language."
                    ),
                }
            ]
            + self.conversation.history()
            + [{"role": "user", "content": text}]
        )

        try:
            reply = self.router.chat("planner", messages, temperature=0.2)
        except Exception as exc:
            self.bus.emit("agent.error", phase="plan", error=str(exc))
            self.conversation.add("user", text)
            print(f"Toner could not reach the model: {exc}")
            return None

        content = str(reply.get("content") or "").strip() or "(no plan produced)"
        self._finish_turn(text, content)
        self.conversation.record_result(Intent.PLAN.value, summary=content[:200])
        print(
            "\nNo files were changed. Ask me to implement it, or use /auto "
            "with plan files in .toner/files_md/."
        )
        return content

    def _handle_code_task(self, text, result):
        if result.follow_up:
            unfinished = self.store.unfinished()

            if unfinished:
                self._finish_turn(
                    text,
                    f"Continuing the active work ({len(unfinished)} "
                    "unfinished task(s)).",
                )
                self.resume()
                return None

            reply = (
                "There is no active task to continue yet. Tell me what to "
                'work on (for example: "fix the login bug") and I will start '
                "a tracked task."
            )
            self._finish_turn(text, reply)
            self.conversation.record_result(result.intent, summary=reply)
            return reply

        spec = {
            "id": "USER",
            "title": text[:70],
            "description": (
                text
                + "\n\nRequested directly in conversation by the user. "
                "Inspect the relevant files first and make the smallest "
                "change that satisfies this request."
            ),
            "dependencies": [],
            "acceptance_criteria": [],
            "checks": [],
            "verification": "Run the relevant checks and report real results.",
            "risk": "medium",
            "require_checks": False,
        }
        task = self.store.add_tasks([spec])[0]
        self._finish_turn(text, f"Starting {task.id}: {task.title}")

        baseline = self.changes.capture()
        outcome = self.execute([task], kind="task", only_ids=[task.id])
        task = self.store.get(task.id) or task
        touched = self._task_touched_paths(task)
        report = self.changes.classify(baseline, touched)
        self.evidence.write(task.id, "changes", report.to_dict())

        task.evidence.append(f"evidence/{task.id}/changes.json")
        self.store.save(task)

        print(f"\n{task.id} finished: {task.status} (run outcome: {outcome})")

        if task.last_error:
            print("Last error:", task.last_error[:300])

        tests = task.last_test or {}
        print(
            "Checks — "
            f"executed: {tests.get('executed', 0)}, "
            f"passed: {tests.get('passed', 0)}, "
            f"failed: {tests.get('failed', 0)}, "
            f"skipped: {tests.get('skipped', 0)}"
        )

        if not tests.get("executed"):
            print("Checks NOT run: no check command was executed.")

        print()
        print("\n".join(report.lines()))

        self.conversation.record_result(
            result.intent,
            task_id=task.id,
            summary=f"{task.status}: {task.title}",
        )
        return report

    def _task_touched_paths(self, task):
        path = self.state_root / "evidence" / task.id / "build.json"

        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        # Evidence files are stored as {"task_id", "name", "data"} envelopes.
        data = payload.get("data") if isinstance(payload, dict) else None

        if not isinstance(data, dict):
            return []

        touched = set(data.get("touched_files") or [])
        touched.update(data.get("new_changes") or [])
        return sorted(touched)

    def _handle_tests(self, text, result):
        commands = []
        candidate = str(text).strip()
        lowered = candidate.lower()

        for prefix in ("please ", "run ", "รัน ", "ช่วยรัน ", "ช่วย "):
            if lowered.startswith(prefix):
                candidate = candidate[len(prefix) :].strip()
                lowered = candidate.lower()

        if candidate and self.registry.policy.is_safe_check(candidate):
            commands.append(candidate)

        configured = str(self.settings.get("test_command", "") or "").strip()

        if configured and configured not in commands:
            commands.append(configured)

        for pending in self.store.unfinished():
            for check in pending.checks:
                if isinstance(check, dict) and check.get("type") == "command":
                    command = str(check.get("command", "")).strip()

                    if command and command not in commands:
                        commands.append(command)

        if not commands:
            reply = (
                "No verification is configured yet. Set test_command in "
                "settings.json, add checks to a task, or give me a specific "
                'allowlisted command to run (for example: "run python3 -m '
                'unittest").'
            )
            self._finish_turn(text, reply)
            self.conversation.record_result(result.intent, summary=reply)
            return reply

        task = Task(
            id="TEST-ADHOC",
            title="Ad-hoc verification",
            description=text,
            checks=[{"type": "command", "command": item} for item in commands],
            risk="low",
            require_checks=False,
        )
        qa_result = self.qa.run(task, [])
        self.evidence.write(task.id, "tests", qa_result.to_dict())

        passed = len(
            [item for item in qa_result.results if item.status == "passed"]
        )
        failed = len(
            [item for item in qa_result.results if item.status == "failed"]
        )
        skipped = len(
            [item for item in qa_result.results if item.status == "skipped"]
        )

        print("\nTEST RESULTS")

        for item in qa_result.results:
            line = f"  [{item.status}] {item.command}"

            if item.exit_code is not None:
                line += f" (exit {item.exit_code})"

            if item.reason:
                line += f" — {item.reason}"

            print(line)

        summary = (
            f"executed {qa_result.executed}, passed {passed}, failed {failed}, "
            f"skipped (not run) {skipped}"
        )
        print("\n" + summary)

        if qa_result.executed == 0:
            print("No checks were actually executed — nothing can pass.")

        self._finish_turn(text, f"Test run complete: {summary}.")
        self.conversation.record_result(
            result.intent, task_id=task.id, summary=summary
        )
        return qa_result

    def _handle_git(self, text, result):
        lowered = text.lower()

        if any(
            word in lowered
            for word in ("push", "merge", "rebase", "reset", "force")
        ):
            reply = (
                "I do not run push/merge/rebase/reset from chat — those are "
                "risky Git operations. Run them yourself, or request them "
                "explicitly as an approved task."
            )
            self._finish_turn(text, reply)
            self.conversation.record_result(result.intent, summary=reply)
            return reply

        if "commit" in lowered:
            return self._git_commit_request(text)

        sections = []
        status = self.registry.git.status()
        sections.append(("git status", status))

        if "diff" in lowered:
            sections.append(("git diff", self.registry.git.diff()))

        if any(word in lowered for word in ("log", "history", "commits", "ประวัติ")):
            sections.append(("git log", self.registry.git.log(10)))

        if "branch" in lowered:
            sections.append(("git branch", self.registry.git.branch()))

        if len(sections) == 1:
            sections.append(("git diff", self.registry.git.diff()))

        lines = []

        for title, payload in sections:
            lines.append(f"{title}:")

            if payload.get("returncode") is None:
                lines.append(
                    f"  unavailable: {payload.get('error') or payload.get('code')}"
                )
            else:
                output = payload.get("output") or ""
                lines.append(output if output else "  (clean / no output)")

            lines.append("")

        reply = "\n".join(lines).strip()
        self._finish_turn(text, reply)
        self.conversation.record_result(result.intent, summary=reply[:200])
        return reply

    def _git_commit_request(self, text):
        changed = self.registry.git_changed_files()

        if changed.get("returncode") is None:
            reply = (
                "Git is unavailable: "
                f"{changed.get('error') or changed.get('code')}"
            )
            self._finish_turn(text, reply)
            return reply

        files = changed.get("files", [])

        if not files:
            reply = "There are no project changes to commit."
            self._finish_turn(text, reply)
            return reply

        listing = "\n  ".join(files[:20]) + (
            f"\n  ... and {len(files) - 20} more" if len(files) > 20 else ""
        )

        if not self.registry.ask(
            f"Toner will commit {len(files)} file(s):\n  {listing}\n"
            "Pre-existing modifications are included. Commit them?",
            always=True,
        ):
            reply = "Commit cancelled; nothing was staged or committed."
            self._finish_turn(text, reply)
            return reply

        message = f"toner: {text[:70]}"
        commit = self.registry.git.commit(message, files=files)

        if commit.get("returncode") == 0:
            reply = (
                f"Committed {str(commit.get('sha') or '')[:12]} — "
                f"{len(files)} file(s): {', '.join(files[:10])}"
            )
        else:
            reply = (
                "Commit failed: "
                f"{commit.get('error') or commit.get('code') or 'unknown error'}"
            )

        self._finish_turn(text, reply)
        self.conversation.record_result(Intent.GIT_TASK.value, summary=reply[:200])
        return reply

    def _handle_clarification(self, text, result):
        reply = (
            result.clarification_question or DEFAULT_CLARIFICATION
        ).strip()
        self._finish_turn(text, reply)
        self.conversation.record_result(result.intent, summary=reply)
        return reply

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
            baseline = self.changes.capture()
            build = self.builder.build(task, run["id"])
        except Exception as exc:
            self.runs.finish(run, "failed", note=str(exc))
            self.bus.emit("agent.error", phase="check", error=str(exc))
            print(f"Toner error: {exc}")
            return

        self.evidence.write(task.id, "build", asdict(build))
        change_report = self.changes.classify(
            baseline, build.touched_files or build.new_changes
        )
        self.evidence.write(
            task.id,
            "diff",
            {
                "changed_files": build.files_changed,
                "new_changes": build.new_changes,
                "changes": change_report.to_dict(),
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
        print()
        print("\n".join(change_report.lines()))
        print(
            f"\nChanged files (Git view): "
            f"{', '.join(build.files_changed) or 'none'}"
        )
        executed_checks = [
            item for item in build.checks_run if item.get("returncode") is not None
        ]
        print(
            "Checks executed: "
            + (
                ", ".join(
                    f"{item.get('command')} (exit {item.get('returncode')})"
                    for item in executed_checks
                )
                or "none"
            )
        )

        if not executed_checks:
            print("Checks NOT run: nothing was executed for this ad-hoc build.")

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
