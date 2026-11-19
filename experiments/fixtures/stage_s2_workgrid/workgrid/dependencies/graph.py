"""The dependency graph: task nodes and the edges between them.

This module owns *structure* only. Whether every reference points somewhere, and whether the
result is acyclic, is :mod:`workgrid.dependencies.validation`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from workgrid.dependencies.errors import (
    DuplicateTaskError,
    InvalidTaskError,
)


@dataclass(frozen=True)
class TaskNode:
    """One task definition, as read from a plan file."""

    task_id: str
    duration: int
    depends_on: tuple[str, ...] = ()
    demand: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, str) or not self.task_id:
            raise InvalidTaskError("task id must be a non-empty string", task=self.task_id)
        if (
            not isinstance(self.duration, int)
            or isinstance(self.duration, bool)
            or self.duration < 1
        ):
            raise InvalidTaskError(
                "task duration must be a positive integer",
                task=self.task_id,
                duration=self.duration,
            )
        for parent in self.depends_on:
            if parent == self.task_id:
                raise InvalidTaskError("a task cannot depend on itself", task=self.task_id)
        for name, units in self.demand.items():
            if not isinstance(units, int) or isinstance(units, bool) or units < 0:
                raise InvalidTaskError(
                    "demand must be a non-negative integer",
                    task=self.task_id,
                    resource=name,
                    units=units,
                )


@dataclass(frozen=True)
class TaskGraph:
    """An immutable graph. ``nodes`` is ordered by ``task_id`` so iteration is stable."""

    nodes: Mapping[str, TaskNode]
    _dependents: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, task_id: object) -> bool:
        return task_id in self.nodes

    def __iter__(self):
        return iter(self.nodes.values())

    def task_ids(self) -> tuple[str, ...]:
        return tuple(self.nodes)

    def node(self, task_id: str) -> TaskNode:
        return self.nodes[task_id]

    def dependencies(self, task_id: str) -> tuple[str, ...]:
        return self.nodes[task_id].depends_on

    def dependents(self, task_id: str) -> tuple[str, ...]:
        return self._dependents.get(task_id, ())

    def roots(self) -> tuple[str, ...]:
        """Tasks with no dependencies, ordered by id."""

        return tuple(tid for tid, node in self.nodes.items() if not node.depends_on)

    def leaves(self) -> tuple[str, ...]:
        """Tasks nothing depends on, ordered by id."""

        return tuple(tid for tid in self.nodes if not self._dependents.get(tid))

    def total_duration(self) -> int:
        return sum(node.duration for node in self.nodes.values())


def build_graph(tasks: Iterable[TaskNode]) -> TaskGraph:
    """Build a graph, rejecting duplicate ids.

    Unknown references are deliberately *not* rejected here: a caller may build a graph first and
    validate it afterwards, and the error type it wants is :class:`UnknownDependencyError`.
    """

    nodes: dict[str, TaskNode] = {}
    for node in tasks:
        if node.task_id in nodes:
            raise DuplicateTaskError("duplicate task id", task=node.task_id)
        nodes[node.task_id] = node

    dependents: dict[str, list[str]] = {tid: [] for tid in nodes}
    for node in nodes.values():
        for parent in node.depends_on:
            dependents.setdefault(parent, []).append(node.task_id)

    return TaskGraph(
        nodes={tid: nodes[tid] for tid in sorted(nodes)},
        _dependents={tid: tuple(sorted(children)) for tid, children in sorted(dependents.items())},
    )
