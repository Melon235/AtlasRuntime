"""Dotted payload paths (v1).

v1 treats the mapping value as a flat key. Nested resolution arrives with the v2 contract.
"""

from __future__ import annotations


def resolve(payload: dict, path: str) -> tuple[bool, object]:
    if path in payload:
        return True, payload[path]
    return False, None
