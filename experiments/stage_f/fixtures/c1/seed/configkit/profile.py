"""Named profiles (v1): none are defined yet."""

from __future__ import annotations

PROFILES: dict[str, dict] = {}


def names() -> list[str]:
    return []


def load(name: str) -> dict:
    return {}
