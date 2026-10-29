"""Task-scoped change attribution.

A baseline is captured before work starts and the final Git state is compared
against it afterwards, so a report can separate:

- files this task actually changed (observed through Toner's own file tools),
- modifications that existed *before* the task,
- untracked files that existed before the task,
- changes that appeared during the task but cannot be confidently attributed
  (for example side effects of a shell command),
- files that were dirty at baseline and are clean now.

This is what stops pre-existing user changes from being reported as output of
the current task. When Git is unavailable the tracker falls back to
tool-observed writes only and says so explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.textutil import utc_now


def parse_porcelain(text):
    """Parse ``git status --porcelain`` into ``{path: status_code}``."""
    entries = {}

    for line in str(text or "").splitlines():
        if len(line) < 4:
            continue

        path = line[3:]

        if " -> " in path:
            path = path.split(" -> ", 1)[1]

        path = path.strip().strip('"')

        if not path:
            continue

        code = (line[0] + line[1]).strip()
        entries[path] = code or "??"

    return entries


@dataclass
class Baseline:
    captured_at: str = ""
    git_available: bool = False
    entries: dict = field(default_factory=dict)
    head: str = ""
    error: str = ""

    def to_dict(self):
        return {
            "captured_at": self.captured_at,
            "git_available": self.git_available,
            "entries": dict(self.entries),
            "head": self.head,
            "error": self.error,
        }


@dataclass
class ChangeReport:
    git_available: bool = True
    changed_by_task: list = field(default_factory=list)
    pre_existing: list = field(default_factory=list)
    pre_existing_untracked: list = field(default_factory=list)
    unattributed: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    overlapped: list = field(default_factory=list)
    limitations: list = field(default_factory=list)

    def to_dict(self):
        return {
            "git_available": self.git_available,
            "changed_by_task": sorted(self.changed_by_task),
            "pre_existing": sorted(self.pre_existing),
            "pre_existing_untracked": sorted(self.pre_existing_untracked),
            "unattributed": sorted(self.unattributed),
            "resolved": sorted(self.resolved),
            "overlapped": sorted(self.overlapped),
            "limitations": list(self.limitations),
        }

    def lines(self):
        """Human-readable sections for the CLI report."""
        out = []

        def section(title, items):
            out.append(f"{title}:")

            if items:
                out.extend(f"  - {item}" for item in sorted(items))
            else:
                out.append("  (none)")

        section("Changed by this task", self.changed_by_task)
        section("Pre-existing changes (not made by this task)", self.pre_existing)
        section(
            "Untracked files that existed before this task",
            self.pre_existing_untracked,
        )
        section(
            "Changes during this task that cannot be attributed",
            self.unattributed,
        )

        if self.overlapped:
            out.append(
                "Files changed by this task that also had pre-existing "
                "modifications:"
            )
            out.extend(f"  - {item}" for item in sorted(self.overlapped))

        for note in self.limitations:
            out.append(f"Limitation: {note}")

        return out


class ChangeTracker:
    def __init__(self, registry):
        self.registry = registry
        self.git = registry.git

    def capture(self):
        result = self.git.porcelain()

        if result.get("returncode") != 0:
            return Baseline(
                captured_at=utc_now(),
                git_available=False,
                error=result.get("error") or result.get("code") or "git unavailable",
            )

        sha = self.git.current_sha()

        return Baseline(
            captured_at=utc_now(),
            git_available=True,
            entries=parse_porcelain(result.get("output", "")),
            head=sha.get("sha", "") if sha.get("returncode") == 0 else "",
        )

    def classify(self, baseline, touched_paths=(), after_text=None):
        baseline = baseline or Baseline()
        touched = {str(path) for path in touched_paths or ()}

        if not baseline.git_available:
            return ChangeReport(
                git_available=False,
                changed_by_task=sorted(touched),
                limitations=[
                    "Git was unavailable at baseline, so only file writes "
                    "observed through Toner's own tools can be attributed; "
                    "other differences are unknown.",
                ],
            )

        if after_text is None:
            result = self.git.porcelain()
            after_text = (
                result.get("output", "")
                if result.get("returncode") == 0
                else ""
            )

        after = parse_porcelain(after_text)
        report = ChangeReport(git_available=True)

        for path, code in sorted(after.items()):
            untracked = code == "??"
            was_dirty = path in baseline.entries

            if not was_dirty:
                if path in touched:
                    report.changed_by_task.append(path)
                else:
                    report.unattributed.append(path)
                continue

            if baseline.entries[path] == code and path not in touched:
                if untracked:
                    report.pre_existing_untracked.append(path)
                else:
                    report.pre_existing.append(path)
                continue

            if path in touched:
                report.changed_by_task.append(path)

                if not untracked:
                    report.overlapped.append(path)
            elif untracked:
                report.pre_existing_untracked.append(path)
            elif baseline.entries[path] != code:
                # Dirty before and still dirty, not touched by Toner: the
                # status letter changed (e.g. staged) without our tools.
                report.unattributed.append(path)
            else:
                report.pre_existing.append(path)

        for path in sorted(set(baseline.entries) - set(after)):
            report.resolved.append(path)

        report.limitations.append(
            "Changes made by shell commands or other processes during the "
            "task are reported as unattributed, not as task output."
        )

        return report
