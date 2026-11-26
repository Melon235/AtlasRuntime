"""Core records shared by every connector (frozen)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """One projected field."""

    name: str
    kind: str
    required: bool = False
    default: Any = None


@dataclass(frozen=True)
class Schema:
    """The record shape a connector projects onto."""

    name: str
    fields: tuple[FieldSpec, ...]

    def field(self, name: str) -> FieldSpec | None:
        for spec in self.fields:
            if spec.name == name:
                return spec
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.fields)

    @property
    def required_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.fields if spec.required)

    @property
    def numeric_names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.fields if spec.kind in ("int", "float"))


@dataclass(frozen=True)
class Record:
    """One projected record."""

    source: str
    line: int
    values: dict[str, Any]


@dataclass(frozen=True)
class ConnectorResult:
    """The records a connector produced plus its resume cursor."""

    records: tuple[Record, ...]
    cursor: int
    skipped: int


@dataclass(frozen=True)
class Batch:
    """A contiguous slice of records."""

    index: int
    records: tuple[Record, ...]
    size_bytes: int = 0

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class RunResult:
    """One pipeline run."""

    source: str
    records: tuple[Record, ...]
    batches: tuple[Batch, ...]
    cursor: int
    skipped: int
    totals: dict[str, float]

    @property
    def record_count(self) -> int:
        return len(self.records)

    @property
    def batch_count(self) -> int:
        return len(self.batches)
