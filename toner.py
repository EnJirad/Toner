"""Toner — reusable AI coding agent and software factory.

This entry point only loads configuration, wires the services and dispatches
input. Slash commands are handled here; everything else is natural language
and goes to the intent router in ``core.orchestrator`` (chat, questions,
inspection, planning, coding tasks, tests and Git requests). Orchestration
lives in ``core.orchestrator``; tools, state and policy live in their own
packages.
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

/help    Show this help
/status  Show the run, task states, attempts, commits and test totals
/check   Read .toner/new_command.md (or /check <text>) and run it ad-hoc
/auto    Read .toner/files_md/*.md, plan tasks, then build/review/test/verify
/resume  Validate saved state, show recovery details and continue safely
/report  Write an evidence-based Markdown engineering report
/stop    Safely cancel the active task and save its state
/exit    Exit Toner (/quit also works)

Anything else is conversation, not a command:
- "hi", "สวัสดี" — normal chat
- "ทำไม build ไม่ผ่าน" — diagnosis with read-only inspection
- "ช่วยแก้บั๊กนี้" — a tracked coding task
- "ตรวจสอบโปรเจกต์ให้หน่อย" — inspect without changing files
- "รัน python3 -m unittest" — run an allowlisted check

Workflow
1. Set project_root in settings.json (or TONER_PROJECT_ROOT).
2. Provide AIUNLIMIT_API_KEY in the environment.
3. Put plan Markdown in <project>/.toner/files_md/ for /auto.
4. Or just type what you want in plain language.

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
            "/report) work; chat and model actions will fail until "
            "AIUNLIMIT_API_KEY is set."
        )

    print("Type /help for commands, or just talk to Toner.\n")


def _dispatch(orchestrator, bus, line):
    """Handle one line of input; return True when Toner should exit."""
    if not line.startswith("/"):
        try:
            orchestrator.handle(line)
        except KeyboardInterrupt:
            request_stop()
            print("\nStop requested; saving state after the current step.")
        except Exception as exc:
            if bus is not None:
                bus.emit("agent.error", phase="chat", error=str(exc))
            print(f"Error: {exc}")
        return False

    name = line.split()[0].lower()
    argument = line[len(name) :].strip()
    lowered = line.lower()

    try:
        if lowered in ("/exit", "/quit"):
            return True
        if name == "/help":
            print(HELP)
        elif name == "/status":
            orchestrator.status()
        elif name == "/check":
            orchestrator.check(prompt=argument or None)
        elif name == "/auto":
            orchestrator.auto()
        elif name == "/resume":
            orchestrator.resume()
        elif name == "/report":
            orchestrator.report()
        elif name == "/stop":
            orchestrator.stop()
        else:
            print(f"Unknown command: {name}. Type /help for the list.")
    except KeyboardInterrupt:
        request_stop()
        print("\nStop requested; saving state after the current step.")
    except Exception as exc:
        if bus is not None:
            bus.emit("agent.error", phase="cli", error=str(exc))
        print(f"Error: {exc}")

    return False


def main():
    signal.signal(signal.SIGINT, request_stop)
    reset_stop()
    orchestrator = None
    bus = None

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
        while True:
            try:
                line = input("Toner > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting Toner.")
                break

            if stop_requested():
                # Ctrl+C at the prompt: the user asked to quit.
                print("\nStop requested; shutting down.")
                break

            if not line:
                continue

            reset_stop()

            if _dispatch(orchestrator, bus, line):
                break

            # An operation cancelled mid-run must leave the session usable.
            reset_stop()
    finally:
        try:
            orchestrator.shutdown()
        except Exception as exc:
            print(f"Shutdown warning: {exc}", file=sys.stderr)

        if bus is not None:
            bus.emit("agent.shutdown")

    print("Toner closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
