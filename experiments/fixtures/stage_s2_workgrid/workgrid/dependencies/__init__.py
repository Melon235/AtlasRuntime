"""Task dependencies: structure, validation, and a stable order.

The published surface is small on purpose. A caller builds or loads a graph, validates it, and asks
for a stable order. Nothing here knows what a worker or a calendar is.
"""

from workgrid.dependencies.errors import (
    CycleDetectedError,
    DependencyError,
    DuplicateTaskError,
    InvalidTaskError,
    UnknownDependencyError,
)
from workgrid.dependencies.graph import TaskGraph, TaskNode, build_graph
from workgrid.dependencies.ordering import (
    check_references,
    dependency_depths,
    find_cycle,
    ready_order,
    topological_order,
)
from workgrid.dependencies.validation import (
    GraphSummary,
    load_graph,
    tasks_from_spec,
    unknown_references,
    validate_graph,
)

__all__ = [
    "CycleDetectedError",
    "DependencyError",
    "DuplicateTaskError",
    "GraphSummary",
    "InvalidTaskError",
    "TaskGraph",
    "TaskNode",
    "UnknownDependencyError",
    "build_graph",
    "check_references",
    "dependency_depths",
    "find_cycle",
    "load_graph",
    "ready_order",
    "tasks_from_spec",
    "topological_order",
    "unknown_references",
    "validate_graph",
]
