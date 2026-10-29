"""Small, dependency-free text, JSON and time helpers shared across Toner."""

from __future__ import annotations

import json
from datetime import datetime, timezone

TRUNCATION_MARKER = "\n... [truncated: {removed} characters omitted] ...\n"


def utc_now():
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def timestamp_slug():
    """Return a filesystem-safe local timestamp with microsecond precision."""
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def json_dumps(value):
    """Serialize ``value`` to JSON, falling back to a string representation."""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        try:
            return json.dumps(str(value), ensure_ascii=False)
        except Exception:
            return '"[unserializable]"'


def json_safe(value):
    """Return a JSON-serializable copy of ``value``."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return json.loads(json_dumps(value))


def truncate_middle(text, limit, marker=TRUNCATION_MARKER):
    """Keep the beginning and end of ``text`` up to ``limit`` characters.

    Test and build failures usually print their most important line first and
    their exit summary last, so dropping only the middle preserves the signal.
    """
    if not isinstance(text, str):
        text = str(text)
    if limit <= 0 or len(text) <= limit:
        return text

    reserved = len(marker.format(removed=len(text)))
    if limit <= reserved + 2:
        return text[:limit]

    available = limit - reserved
    head = available // 2
    tail = available - head
    removed = len(text) - head - tail
    return text[:head] + marker.format(removed=removed) + text[-tail:]


def extract_json_object(text):
    """Extract the first JSON object from model output.

    Handles bare JSON, Markdown-fenced JSON and leading/trailing prose without
    inventing or repairing content. Raises ``ValueError`` when no parseable
    object can be found.
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty response")

    candidate = text.strip()

    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        data = json.loads(candidate)
    except ValueError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in response")
        data = json.loads(candidate[start:end + 1])

    if not isinstance(data, dict):
        raise ValueError("response JSON is not an object")
    return data
