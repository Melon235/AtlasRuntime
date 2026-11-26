"""Merge (v2): explicit > env > file > default with provenance."""

from __future__ import annotations

from dataclasses import dataclass

from configkit.document import flatten

LAYERS = ("default", "file", "profile", "env", "explicit")


@dataclass(frozen=True)
class Resolved:
    values: dict
    origins: dict


def resolve(defaults, file_values, env_values, explicit, profile_values=None) -> Resolved:
    layers = (
        ("default", defaults),
        ("file", file_values),
        ("profile", profile_values or {}),
        ("env", env_values),
        ("explicit", explicit),
    )
    values: dict = {}
    origins: dict = {}
    for name, layer in layers:
        for key, value in flatten(layer).items():
            values[key] = value
            origins[key] = name
    return Resolved(
        values=dict(sorted(values.items())),
        origins=dict(sorted(origins.items())),
    )


__all__ = ["LAYERS", "Resolved", "resolve"]
