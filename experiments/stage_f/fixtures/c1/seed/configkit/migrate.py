"""Migration (v1): no-op."""

from __future__ import annotations


def upgrade(document: dict) -> dict:
    return dict(document)
