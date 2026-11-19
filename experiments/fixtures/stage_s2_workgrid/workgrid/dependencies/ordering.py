"""Stable ordering over a dependency graph.

"Stable" here has a precise meaning: for a fixed graph the order is a single tuple, and tasks
that are simultaneously ready are ordered by ``task_id``. No dependence on insertion order, dict
iteration or ``hash()`` is allowed, so two runs of the same plan are byte-identical.
"""

from __future__ import annotations

from workgrid.dependencies.errors import (
    CycleDetectedError,
    UnknownDependencyError,
)
from workgrid.dependencies.graph import TaskGraph


def check_references(graph: TaskGraph) -> None:
    """Raise :class:`UnknownDependencyError` for the first dangling reference, by task id."""

    for task_id in graph.task_ids():
        for parent in graph.dependencies(task_id):
            if parent not in graph:
                raise UnknownDependencyError(
                    f"task {task_id!r} depends on unknown task {parent!r}",
                    task=task_id,
                    missing=parent,
                )


def topological_order(graph: TaskGraph) -> tuple[str, ...]:
    """Kahn's algorithm with a deterministic ready set: the smallest task id goes first."""

    check_references(graph)
    remaining = {task_id: set(graph.dependencies(task_id)) for task_id in graph.task_ids()}
    ready = sorted(tid for tid, parents in remaining.items() if not parents)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        del remaining[current]
        for child in graph.dependents(current):
            if child not in remaining:
                continue
            remaining[child].discard(current)
            if not remaining[child] and child not in ready:
                ready.append(child)
                ready.sort()
    if len(order) != len(graph):
        raise CycleDetectedError(find_cycle(graph))
    return tuple(order)


def find_cycle(graph: TaskGraph) -> tuple[str, ...]:
    """A deterministic witness cycle: the smallest cycle in id order, closed on its first node."""

    for start in graph.task_ids():
        path = _walk_to_self(graph, start)
        if path is not None:
            return path
    return ()


def _walk_to_self(graph: TaskGraph, start: str) -> tuple[str, ...] | None:
    """A path ``start -> ... -> node`` where ``node`` depends on ``start``, or ``None``."""

    stack: list[tuple[str, tuple[str, ...]]] = [(start, (start,))]
    seen: set[str] = set()
    while stack:
        current, path = stack.pop()
        for parent in sorted(graph.dependencies(current)):
            if parent == start:
                return (*path, start)
            if parent in seen or parent not in graph:
                continue
            seen.add(parent)
            stack.append((parent, (*path, parent)))
    return None


def dependency_depths(graph: TaskGraph) -> dict[str, int]:
    """Depth of each task: 0 for roots, otherwise one more than its deepest dependency."""

    depths: dict[str, int] = {}
    for task_id in topological_order(graph):
        parents = graph.dependencies(task_id)
        depths[task_id] = 0 if not parents else 1 + max(depths[parent] for parent in parents)
    return depths


def ready_order(graph: TaskGraph) -> tuple[str, ...]:
    """The order in which the engine considers tasks: topological, ties by task id."""

    return topological_order(graph)
