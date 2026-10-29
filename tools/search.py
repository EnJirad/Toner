"""Plain-text project search constrained by the security policy."""

from __future__ import annotations

from policy.security import SecurityError
from tools.filesystem import walk_files


class SearchTools:
    def __init__(self, policy):
        self.policy = policy

    def search_code(self, query, directory="."):
        if not isinstance(query, str) or not query:
            return {"error": "query must be a non-empty string", "code": "invalid_query"}

        try:
            base = self.policy.resolve(directory)
        except SecurityError as exc:
            return {"error": str(exc), "code": exc.code}

        if not base.is_dir():
            return {"error": "Not a directory", "code": "not_a_directory"}

        needle = query.casefold()
        matches = []

        for path in walk_files(self.policy, base):
            try:
                if path.stat().st_size > self.policy.max_file_bytes:
                    continue

                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue

            for number, line in enumerate(lines, 1):
                if needle in line.casefold():
                    matches.append(
                        {
                            "file": str(path.relative_to(self.policy.root)),
                            "line": number,
                            "text": line[:300],
                        }
                    )

                    if len(matches) >= self.policy.max_search_results:
                        return {"matches": matches, "truncated": True}

        return {"matches": matches, "truncated": False}
