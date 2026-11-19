"""Compatibility suite for the dependency package.

These tests describe behaviour that callers already rely on. They must keep passing unchanged.
"""

from __future__ import annotations

import pytest
from workgrid.dependencies import (
    CycleDetectedError,
    DuplicateTaskError,
    InvalidTaskError,
    UnknownDependencyError,
    build_graph,
    dependency_depths,
    find_cycle,
    load_graph,
    tasks_from_spec,
    topological_order,
    validate_graph,
)
from workgrid.dependencies.graph import TaskNode


def node(task_id: str, duration: int = 1, depends_on: tuple[str, ...] = ()) -> TaskNode:
    return TaskNode(task_id=task_id, duration=duration, depends_on=depends_on)


# -- structure ---------------------------------------------------------------


def test_a_self_dependency_is_invalid() -> None:
    with pytest.raises(InvalidTaskError):
        node("a", depends_on=("a",))


def test_a_duration_must_be_positive() -> None:
    with pytest.raises(InvalidTaskError):
        node("a", duration=0)


def test_a_boolean_duration_is_not_an_integer() -> None:
    with pytest.raises(InvalidTaskError):
        TaskNode(task_id="a", duration=True)  # type: ignore[arg-type]


def test_an_empty_task_id_is_invalid() -> None:
    with pytest.raises(InvalidTaskError):
        node("")


def test_duplicate_ids_are_rejected() -> None:
    with pytest.raises(DuplicateTaskError) as error:
        build_graph([node("a"), node("a", duration=2)])
    assert error.value.as_dict()["task"] == "a"


def test_nodes_iterate_in_id_order() -> None:
    graph = build_graph([node("c"), node("a"), node("b")])
    assert graph.task_ids() == ("a", "b", "c")


def test_roots_and_leaves() -> None:
    graph = build_graph([node("a"), node("b", depends_on=("a",)), node("c", depends_on=("a",))])
    assert graph.roots() == ("a",)
    assert graph.leaves() == ("b", "c")
    assert graph.dependents("a") == ("b", "c")


def test_total_duration() -> None:
    graph = build_graph([node("a", duration=3), node("b", duration=4)])
    assert graph.total_duration() == 7


# -- ordering ----------------------------------------------------------------


def test_topological_order_puts_dependencies_first() -> None:
    graph = build_graph([node("b", depends_on=("a",)), node("a")])
    assert topological_order(graph) == ("a", "b")


def test_simultaneously_ready_tasks_are_ordered_by_id() -> None:
    graph = build_graph(
        [
            node("c", depends_on=("a", "b")),
            node("b"),
            node("a"),
        ]
    )
    assert topological_order(graph) == ("a", "b", "c")


def test_the_order_is_repeatable() -> None:
    graph = build_graph([node("d", depends_on=("b", "c")), node("c"), node("b"), node("a")])
    assert topological_order(graph) == topological_order(graph)


def test_depths() -> None:
    graph = build_graph([node("a"), node("b", depends_on=("a",)), node("c", depends_on=("b",))])
    assert dependency_depths(graph) == {"a": 0, "b": 1, "c": 2}


# -- validation --------------------------------------------------------------


def test_an_unknown_dependency_is_reported() -> None:
    graph = build_graph([node("a", depends_on=("ghost",))])
    with pytest.raises(UnknownDependencyError) as error:
        validate_graph(graph)
    assert error.value.as_dict()["missing"] == "ghost"


def test_a_cycle_is_detected_and_witnessed() -> None:
    graph = build_graph(
        [node("a", depends_on=("c",)), node("b", depends_on=("a",)), node("c", depends_on=("b",))]
    )
    with pytest.raises(CycleDetectedError) as error:
        validate_graph(graph)
    cycle = error.value.cycle
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"a", "b", "c"}


def test_find_cycle_returns_empty_for_a_dag() -> None:
    graph = build_graph([node("a"), node("b", depends_on=("a",))])
    assert find_cycle(graph) == ()


def test_a_valid_graph_is_summarized() -> None:
    graph = build_graph(
        [
            node("a", duration=2),
            node("b", duration=1, depends_on=("a",)),
            node("c", duration=4),
        ]
    )
    summary = validate_graph(graph)
    assert summary.as_dict() == {
        "task_count": 3,
        "roots": ["a", "c"],
        "leaves": ["b", "c"],
        "max_depth": 1,
        "total_duration": 7,
    }


def test_an_empty_graph_is_valid() -> None:
    graph, summary = load_graph([])
    assert len(graph) == 0
    assert summary.task_count == 0
    assert summary.max_depth == 0


# -- reading a document ------------------------------------------------------


def test_the_task_list_is_read_with_defaults() -> None:
    nodes = tasks_from_spec([{"id": "a", "duration": 2}])
    assert nodes[0].depends_on == ()
    assert nodes[0].demand == {}


def test_a_missing_field_is_an_invalid_task_not_a_key_error() -> None:
    with pytest.raises(InvalidTaskError):
        tasks_from_spec([{"id": "a"}])


def test_a_string_dependency_list_is_rejected() -> None:
    with pytest.raises(InvalidTaskError):
        tasks_from_spec([{"id": "a", "duration": 1, "depends_on": "b"}])


def test_demand_is_read_as_integers() -> None:
    nodes = tasks_from_spec([{"id": "a", "duration": 1, "demand": {"workers": 2}}])
    assert nodes[0].demand == {"workers": 2}
