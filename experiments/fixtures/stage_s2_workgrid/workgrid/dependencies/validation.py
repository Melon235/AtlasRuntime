"""Validating a plan before anyone tries to schedule it.

:func:`validate_graph` answers one question -- "is this a sound dependency graph?" -- and returns a
small summary a caller can print. It never repairs anything, and it never turns a broken graph into
a partial result.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from workgrid.dependencies.errors import (
    CycleDetectedError,
    InvalidTaskError,
    UnknownDependencyError,
)
from workgrid.dependencies.graph import TaskGraph, TaskNode, build_graph
from workgrid.dependencies.ordering import (
    check_references,
    dependency_depths,
    find_cycle,
    topological_order,
)


@dataclass(frozen=True)
class GraphSummary:
    """What a caller may believe about a graph that validated."""

    task_count: int
    roots: tuple[str, ...]
    leaves: tuple[str, ...]
    max_depth: int
    total_duration: int

    def as_dict(self) -> dict[str, object]:
        return {
            "task_count": self.task_count,
            "roots": list(self.roots),
            "leaves": list(self.leaves),
            "max_depth": self.max_depth,
            "total_duration": self.total_duration,
        }


def validate_graph(graph: TaskGraph) -> GraphSummary:
    """Raise on a dangling reference or a cycle; otherwise describe the graph."""

    check_references(graph)
    order = topological_order(graph)
    if len(order) != len(graph):
        raise CycleDetectedError(find_cycle(graph))
    depths = dependency_depths(graph)
    return GraphSummary(
        task_count=len(graph),
        roots=graph.roots(),
        leaves=graph.leaves(),
        max_depth=max(depths.values()) if depths else 0,
        total_duration=graph.total_duration(),
    )


def tasks_from_spec(spec: Iterable[Mapping[str, object]]) -> tuple[TaskNode, ...]:
    """Read the ``tasks`` list of a plan document.

    Unknown keys are ignored so a plan may carry presentation metadata; a missing ``id`` or
    ``duration`` is an :class:`InvalidTaskError`, not a ``KeyError``.
    """

    nodes: list[TaskNode] = []
    for index, raw in enumerate(spec):
        if not isinstance(raw, Mapping):
            raise InvalidTaskError("each task must be a mapping", index=index)
        try:
            task_id = raw["id"]
            duration = raw["duration"]
        except KeyError as exc:
            raise InvalidTaskError(
                "a task needs 'id' and 'duration'",
                index=index,
                missing=str(exc),
            ) from exc
        depends_on = raw.get("depends_on") or ()
        if isinstance(depends_on, str):
            raise InvalidTaskError(
                "depends_on must be a list of task ids, not a string",
                task=task_id,
            )
        demand = raw.get("demand") or {}
        if not isinstance(demand, Mapping):
            raise InvalidTaskError("demand must be a mapping of resource to units", task=task_id)
        nodes.append(
            TaskNode(
                task_id=str(task_id),
                duration=duration,  # type: ignore[arg-type]
                depends_on=tuple(str(parent) for parent in depends_on),
                demand={str(name): int(units) for name, units in demand.items()},
            )
        )
    return tuple(nodes)


def load_graph(spec: Iterable[Mapping[str, object]]) -> tuple[TaskGraph, GraphSummary]:
    """Build and validate in one step, which is what both callers actually need."""

    graph = build_graph(tasks_from_spec(spec))
    return graph, validate_graph(graph)


def unknown_references(graph: TaskGraph) -> tuple[tuple[str, str], ...]:
    """Every dangling reference, ordered by (task, missing) -- for reporting, not for raising."""

    found = [
        (task_id, parent)
        for task_id in graph.task_ids()
        for parent in graph.dependencies(task_id)
        if parent not in graph
    ]
    return tuple(found)


__all__ = [
    "CycleDetectedError",
    "GraphSummary",
    "UnknownDependencyError",
    "load_graph",
    "tasks_from_spec",
    "unknown_references",
    "validate_graph",
]
