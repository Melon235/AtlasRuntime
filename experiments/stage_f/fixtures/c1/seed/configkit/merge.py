"""Merge (v1): file wins over the environment, which wins over the explicit layer."""

from __future__ import annotations


def resolve(defaults, file_values, env_values, explicit) -> dict:
    merged = dict(defaults)
    merged.update(explicit)
    merged.update(env_values)
    merged.update(file_values)
    return merged
