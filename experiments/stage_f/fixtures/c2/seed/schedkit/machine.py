"""State machine (v1): no cancel, no exhaust, no recovery event."""

from __future__ import annotations

from schedkit.errors import InvalidTransition
from schedkit.model import STATES, TERMINAL

_TABLE = {
    ("queued", "start"): "running",
    ("running", "finish"): "done",
    ("running", "fail"): "failed",
}


def transition(state: str, event: str) -> str:
    if state not in STATES:
        raise InvalidTransition(f"unknown state {state!r}")
    try:
        return _TABLE[(state, event)]
    except KeyError as error:
        raise InvalidTransition(f"{event!r} is not allowed from {state!r}") from error


__all__ = ["STATES", "TERMINAL", "transition"]
