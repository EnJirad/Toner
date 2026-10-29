"""Process-wide cooperative stop flag.

The CLI installs signal handlers that call :func:`request_stop`; long-running
work checks :func:`stop_requested` between expensive steps so state can be
saved and the run can be resumed later.
"""

from __future__ import annotations

_STOP_REQUESTED = False


def request_stop(signum=None, frame=None):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


def stop_requested():
    return _STOP_REQUESTED


def reset_stop():
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
