"""Toner — reusable AI coding agent and software factory.

This entry point only loads configuration, wires the services and dispatches
CLI commands. Orchestration lives in ``core.orchestrator``; tools, state and
policy live in their own packages.
"""

from __future__ import annotations

import signal
import sys

from core.config import load_settings
from core.interrupts import request_stop, reset_stop, stop_requested
from core.model_router import ModelRouter
from core.orchestrator import Orchestrator
from events.event_bus import EventBus
from tools.registry import ToolRegistry

HELP = """
Toner commands

/check   Read .toner/new_command.md and run it as one ad-hoc task
/auto    Read .toner/files_md/*.md, plan tasks, then build/review/test/verify
/status  Show the run, task states, attempts, commits and test totals
/resume  Validate saved state, show recovery details and continue safely
/report  Write an evidence-based Markdown engineering report
/help    Show this help
/exit    Exit Toner

Workflow
1. Set project_root in settings.json (or TONER_PROJECT_ROOT).
2. Provide AIUNLIMIT_API_KEY in the environment.
3. Put plan Markdown in <project>/.toner/files_md/.
4. Use /auto for the plan, or /check for a single command.

A task is only marked DONE after the Verifier accepts executed tests, the
review verdict and every machine-checkable acceptance criterion. Evidence is
written to <project>/.toner/evidence/<task>/.
""".strip()


def _banner(settings, orchestrator):
    print("=" * 56)
    print("TONER — Reusable AI Coding Agent")
    print("=" * 56)
    print("Model:", settings.get("model"))
    print("Project:", orchestrator.root)
    print("State:", orchestrator.state_root)
    print("Evidence:", orchestrator.state_root / "evidence")

    if not settings.get("api_key"):
        print(
            "\n⚠️ No API key configured. Local commands (/status, /resume, "
            "/report) work; model actions will fail until AIUNLIMIT_API_KEY "
            "is set."
        )

    print("Type /help for commands.\n")


def main():
    signal.signal(signal.SIGINT, request_stop)
    reset_stop()
    orchestrator = None
    bus = None
    exit_code = 0

    try:
        settings = load_settings()
        registry = ToolRegistry(settings)
        bus = EventBus(registry.state_root / "state" / "events.jsonl")
        router = ModelRouter(settings)
        orchestrator = Orchestrator(
            settings,
            registry=registry,
            router=router,
            bus=bus,
        )
    except Exception as exc:
        print(f"Toner failed to start: {exc}")
        return 1

    _banner(settings, orchestrator)

    try:
        while not stop_requested():
            try:
                command = input("Toner > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting Toner.")
                break

            if not command:
                continue

            lowered = command.lower()

            try:
                if lowered in ("/exit", "exit", "quit"):
                    break
                if lowered == "/help":
                    print(HELP)
                elif lowered == "/status":
                    orchestrator.status()
                elif lowered == "/check":
                    orchestrator.check()
                elif lowered == "/auto":
                    orchestrator.auto()
                elif lowered == "/resume":
                    orchestrator.resume()
                elif lowered == "/report":
                    orchestrator.report()
                else:
                    orchestrator.check(prompt=command)
            except KeyboardInterrupt:
                request_stop()
                print("\nStop requested; saving state after the current step.")
            except Exception as exc:
                if bus is not None:
                    bus.emit("agent.error", phase="cli", error=str(exc))
                print(f"Error: {exc}")
    finally:
        try:
            orchestrator.shutdown()
        except Exception as exc:
            print(f"Shutdown warning: {exc}", file=sys.stderr)

        if bus is not None:
            bus.emit("agent.shutdown")

    print("Toner closed.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
