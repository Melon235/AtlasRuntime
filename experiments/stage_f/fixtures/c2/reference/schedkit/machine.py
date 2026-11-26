"""State machine (v2): the complete table, terminal states, typed failures."""

from __future__ import annotations

from schedkit.errors import InvalidTransition
from schedkit.model import STATES, TERMINAL

_TABLE: dict[str, dict[str, str]] = {
    "queued": {"start": "running", "cancel": "cancelled"},
    "running": {
        "finish": "done",
        "fail": "failed",
        "cancel": "cancelled",
        "recover": "queued",
    },
    "failed": {"retry": "queued", "exhaust": "dead", "cancel": "cancelled"},
    "done": {},
    "dead": {},
    "cancelled": {},
}


def transition(state: str, event: str) -> str:
    if state not in STATES:
        raise InvalidTransition(f"unknown state {state!r}")
    if event not in _TABLE[state]:
        raise InvalidTransition(f"{event!r} is not allowed from {state!r}")
    return _TABLE[state][event]


__all__ = ["STATES", "TERMINAL", "transition"]
