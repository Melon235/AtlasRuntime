"""Source registry (v2): both connectors, duplicates rejected, sorted listing."""

from __future__ import annotations

from . import csv_connector, jsonl_connector

_FACTORIES: dict[str, object] = {}


def register(name: str, factory: object) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("source name must be a non-empty string")
    if name in _FACTORIES:
        raise ValueError(f"source {name!r} is already registered")
    _FACTORIES[name] = factory


def build(name: str):
    return _FACTORIES[name]


def available() -> list[str]:
    return sorted(_FACTORIES)


register("csv", csv_connector.read)
register("jsonl", jsonl_connector.read)
