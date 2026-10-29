"""Backward-compatible facade for Toner's tool layer.

Earlier versions of Toner exposed every tool from this module. The
implementation now lives in the ``tools`` and ``policy`` packages; importing
``AgentTools`` from here keeps existing integrations working unchanged.
"""

from __future__ import annotations

from core.textutil import utc_now
from policy.security import (
    DANGEROUS_PATTERNS,
    SECRET_ENV_WORDS,
    SECRET_NAMES,
    SECRET_SUFFIXES,
    is_secret,
)
from tools.registry import ToolRegistry, make_tool_schema

AgentTools = ToolRegistry

__all__ = [
    "AgentTools",
    "DANGEROUS_PATTERNS",
    "SECRET_ENV_WORDS",
    "SECRET_NAMES",
    "SECRET_SUFFIXES",
    "ToolRegistry",
    "is_secret",
    "make_tool_schema",
    "utc_now",
]
