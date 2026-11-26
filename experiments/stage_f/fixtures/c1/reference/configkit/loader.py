"""Sources (v2): the CONFIGKIT__ environment mapping."""

from __future__ import annotations

from collections.abc import Mapping

from configkit.document import parse
from configkit.errors import ParseError

PREFIX = "CONFIGKIT__"
SEPARATOR = "__"


def read_env(env_values: Mapping[str, str]) -> dict[str, str]:
    mapped: dict[str, str] = {}
    for key, value in env_values.items():
        if not isinstance(key, str) or not key.startswith(PREFIX):
            continue
        if not isinstance(value, str):
            raise ParseError(key, "environment values must be strings")
        raw = key[len(PREFIX) :]
        if not raw:
            raise ParseError(key, "environment key has an empty path")
        parts = [part.lower() for part in raw.split(SEPARATOR)]
        if any(not part for part in parts):
            raise ParseError(key, "environment key has an empty path segment")
        mapped[".".join(parts)] = value
    return mapped


def load(file_text: str, env_values: Mapping[str, str]) -> dict:
    values = parse(file_text)
    values.update(read_env(env_values))
    return values
