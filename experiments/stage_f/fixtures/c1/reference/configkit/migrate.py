"""Migration (v2): consume ``version``, rename v1 keys, drop legacy keys."""

from __future__ import annotations

from configkit.document import flatten
from configkit.errors import MigrationError
from configkit.spec import LEGACY_PREFIX, RENAMES_V1, VERSION


def upgrade(document: dict) -> dict:
    values = flatten(document)
    version = values.pop("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise MigrationError("version", f"version must be an integer, got {version!r}")
    if version not in (1, VERSION):
        raise MigrationError("version", f"unsupported configuration version {version}")
    if version == VERSION:
        return dict(sorted(values.items()))

    renamed: dict = {}
    for key, value in values.items():
        if key.startswith(LEGACY_PREFIX):
            continue
        target = RENAMES_V1.get(key, key)
        if target != key and target in values:
            continue
        renamed[target] = value
    return dict(sorted(renamed.items()))


__all__ = ["upgrade"]
