"""Validation (v1): coerces known numbers and ignores everything else."""

from __future__ import annotations

from configkit.spec import FIELDS


def check(values: dict) -> dict:
    checked = {}
    for key, value in values.items():
        spec = FIELDS.get(key)
        if spec is None:
            continue
        if spec.kind == "int":
            checked[key] = int(value)
        elif spec.kind == "float":
            checked[key] = float(value)
        else:
            checked[key] = value
    return checked
