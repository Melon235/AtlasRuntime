"""Frozen v2 audit API.

The v1 positional form is gone. Call sites must use keywords and consume a ``Receipt`` rather than a
dict. This module is not edited while the migration runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Receipt:
    """One recorded audit event."""

    kind: str
    data: dict[str, Any]
    actor: str | None


def record(*, kind: str, data: dict[str, Any], actor: str | None = None) -> Receipt:
    """Record one event and return its receipt."""

    if not isinstance(kind, str) or not kind:
        raise ValueError("kind must be a non-empty string")
    if not isinstance(data, dict):
        raise ValueError("data must be a dict")
    if actor is not None and (not isinstance(actor, str) or not actor):
        raise ValueError("actor must be None or a non-empty string")
    return Receipt(kind=kind, data=dict(data), actor=actor)
