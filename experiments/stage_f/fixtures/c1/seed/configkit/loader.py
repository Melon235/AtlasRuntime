"""Sources (v1): environment keys are used verbatim."""

from __future__ import annotations

from collections.abc import Mapping

from configkit.document import parse


def read_env(env_values: Mapping[str, str]) -> dict[str, str]:
    return dict(env_values)


def load(file_text: str, env_values: Mapping[str, str]) -> dict:
    values = parse(file_text)
    values.update(read_env(env_values))
    return values
