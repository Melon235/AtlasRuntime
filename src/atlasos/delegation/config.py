"""Stage M delegation configuration.

Deliberately **not** a field of :class:`atlasos.config.AtlasConfig`.

``AtlasConfig.model_dump()`` is hashed into every frozen experiment and dry-run identity
(``experiments/evaluation/runner.py::_config_digest``). Adding a section there would silently change
those recorded digests for every existing configuration, which is exactly what a historical
evidence artifact must not do. Stage M therefore owns its own small section and its own loader:
a path that does not ask for delegation reads nothing and changes nothing.

Ablation switch: ``delegation.enabled = false`` (or a run that never calls ``run_goal``) leaves the
workflow path and every existing entry point byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DelegationConfig(BaseModel):
    """Limits for the Main-driven delegation plane."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    #: Upper bound on children running at the same time. Deliberately independent of the AnyCode
    #: agent pool: the Main already holds the pool slot its tool call runs in.
    max_parallel_children: int = Field(default=2, ge=1, le=4)
    #: Upper bound on candidates in one ``delegate_tasks`` call.
    max_children_per_call: int = Field(default=2, ge=1, le=4)
    #: Upper bound on ``delegate_tasks`` calls per run.
    max_delegation_rounds: int = Field(default=4, ge=1, le=16)
    child_timeout_seconds: float = Field(default=300.0, gt=0, le=1800)
    child_max_turns: int = Field(default=6, ge=1, le=20)
    #: Per-child summary characters returned to the Main; the full output stays behind a reference.
    result_preview_chars: int = Field(default=900, ge=200, le=4000)

    @model_validator(mode="after")
    def _validate_limits(self) -> DelegationConfig:
        if self.max_children_per_call > self.max_parallel_children:
            raise ValueError(
                "delegation.max_children_per_call must not exceed max_parallel_children: the "
                "admission router refuses a wave wider than the concurrency limit"
            )
        return self


def load_delegation_config(path: str | Path) -> DelegationConfig:
    """Read the optional ``delegation:`` section of a configuration file.

    A file without the section keeps the shipped defaults; an unknown key inside the section is
    rejected, so a typo cannot silently disable a limit.
    """

    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    section: Any = payload.get("delegation") or {}
    if not isinstance(section, dict):
        raise ValueError("the 'delegation' section must be a mapping")
    return DelegationConfig.model_validate(section)


__all__ = ["DelegationConfig", "load_delegation_config"]
