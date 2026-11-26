"""Source registry (v1).

v1 knows the CSV connector only and a duplicate registration silently replaces the previous entry.
"""

from __future__ import annotations

from . import csv_connector

_FACTORIES: dict[str, object] = {}


def register(name: str, factory: object) -> None:
    _FACTORIES[name] = factory


def build(name: str):
    return _FACTORIES[name]


def available() -> list[str]:
    return list(_FACTORIES)


register("csv", csv_connector.read)
