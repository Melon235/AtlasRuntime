"""TOML document parsing (v1): flattens only one table level."""

from __future__ import annotations

import tomllib


def flatten(data: dict) -> dict:
    flat: dict = {}
    for key, value in data.items():
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                flat[f"{key}.{inner}"] = inner_value
        else:
            flat[key] = value
    return flat


def parse(text: str) -> dict:
    return flatten(tomllib.loads(text))
