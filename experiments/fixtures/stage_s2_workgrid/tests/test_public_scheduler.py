"""Acceptance suite for the shared scheduling layer.

This file is the public statement of what "a schedule" means: status, order, resource packing,
unschedulable classification and the text form. It is the suite the Host runs as
``run_public_tests``; the independent acceptance check is not this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from workgrid.dependencies import build_graph
from workgrid.dependencies.graph import TaskNode
from workgrid.resources import (
    AvailabilityWindow,
    Calendar,
    PoolSet,
    ResourceConstraints,
    ResourcePool,
)
from workgrid.scheduler import PlanStatus, ReasonCode, build_schedule, render_plan

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "plan.json"


def task(
    task_id: str, duration: int = 1, depends_on: tuple[str, ...] = (), demand=None
) -> TaskNode:
    return TaskNode(
        task_id=task_id,
        duration=duration,
        depends_on=depends_on,
        demand=demand or {},
    )


def constraints(
    workers: int = 1, windows=((0, 10),), gpu: int | None = None
) -> ResourceConstraints:
    pools = [ResourcePool("workers", workers)]
    if gpu is not None:
        pools.append(ResourcePool("gpu", gpu))
    calendar = Calendar(tuple(AvailabilityWindow(start, end) for start, end in windows))
    return ResourceConstraints(pools=PoolSet(tuple(pools)), calendar=calendar)


# -- a plan that fits --------------------------------------------------------


def test_a_chain_runs_in_dependency_order() -> None:
    plan = build_schedule(
        build_graph([task("a", 2), task("b", 2, depends_on=("a",))]),
        constraints(workers=1),
    )
    assert plan.status is PlanStatus.SCHEDULED
    assert [(item.task_id, item.start, item.end) for item in plan.tasks] == [
        ("a", 0, 2),
        ("b", 2, 4),
    ]
    assert plan.makespan == 4
    assert plan.summary == {
        "makespan": 4,
        "scheduled": 2,
        "tasks": 2,
        "unschedulable": 0,
        "usable_slots": 10,
    }


def test_capacity_forces_sequential_execution() -> None:
    plan = build_schedule(
        build_graph([task("a", 2, demand={"workers": 1}), task("b", 2, demand={"workers": 1})]),
        constraints(workers=1),
    )
    assert [(item.task_id, item.start) for item in plan.tasks] == [("a", 0), ("b", 2)]


def test_two_workers_run_in_parallel() -> None:
    plan = build_schedule(
        build_graph([task("a", 2, demand={"workers": 1}), task("b", 2, demand={"workers": 1})]),
        constraints(workers=2),
    )
    assert [(item.task_id, item.start, item.end) for item in plan.tasks] == [
        ("a", 0, 2),
        ("b", 0, 2),
    ]
    assert plan.makespan == 2
    assert dict(plan.peak_usage) == {"workers": 2}


def test_a_closed_gap_pushes_the_start() -> None:
    plan = build_schedule(
        build_graph([task("a", 3)]),
        constraints(workers=1, windows=((0, 2), (4, 10))),
    )
    assert [(item.task_id, item.start, item.end) for item in plan.tasks] == [("a", 4, 7)]


def test_a_multi_resource_task_waits_for_the_scarce_one() -> None:
    plan = build_schedule(
        build_graph(
            [
                task("a", 1, demand={"workers": 1}),
                task("b", 1, demand={"workers": 1, "gpu": 1}),
                task("c", 1, demand={"workers": 1, "gpu": 1}, depends_on=("a",)),
            ]
        ),
        constraints(workers=2, gpu=1),
    )
    assert [(item.task_id, item.start) for item in plan.tasks] == [("a", 0), ("b", 0), ("c", 1)]


def test_an_empty_plan_is_empty() -> None:
    plan = build_schedule(build_graph([]), constraints())
    assert plan.status is PlanStatus.EMPTY
    assert plan.tasks == ()
    assert plan.makespan == 0
    assert json.loads(json.dumps(plan.to_dict()))["status"] == "empty"


# -- a plan that cannot be completed ----------------------------------------


def test_demand_above_capacity_is_unschedulable() -> None:
    plan = build_schedule(
        build_graph([task("a", 1, demand={"workers": 3}), task("b", 1)]),
        constraints(workers=2),
    )
    assert plan.status is PlanStatus.PARTIAL
    assert plan.unschedulable_ids() == ("a",)
    assert plan.unschedulable[0].reason == ReasonCode.DEMAND_EXCEEDS_CAPACITY
    assert plan.scheduled_ids() == ("b",)


def test_an_unknown_resource_is_unschedulable() -> None:
    plan = build_schedule(
        build_graph([task("a", 1, demand={"memory": 1})]),
        constraints(workers=1),
    )
    assert plan.unschedulable[0].reason == ReasonCode.UNKNOWN_RESOURCE


def test_a_task_longer_than_every_window_is_unschedulable() -> None:
    plan = build_schedule(build_graph([task("a", 5)]), constraints(workers=1, windows=((0, 4),)))
    assert plan.unschedulable[0].reason == ReasonCode.NO_AVAILABLE_WINDOW


def test_a_dependent_of_an_unschedulable_task_is_unschedulable() -> None:
    plan = build_schedule(
        build_graph([task("a", 1, demand={"workers": 4}), task("b", 1, depends_on=("a",))]),
        constraints(workers=2),
    )
    assert plan.unschedulable_ids() == ("a", "b")
    assert [item.reason for item in plan.unschedulable] == [
        ReasonCode.DEMAND_EXCEEDS_CAPACITY,
        ReasonCode.DEPENDENCY_UNSCHEDULABLE,
    ]


# -- the published forms -----------------------------------------------------


def test_the_dict_form_is_canonical() -> None:
    plan = build_schedule(
        build_graph([task("a", 2), task("b", 1, depends_on=("a",), demand={"gpu": 1})]),
        constraints(workers=1, gpu=1),
    )
    document = plan.to_dict()
    assert list(document) == [
        "status",
        "makespan",
        "summary",
        "peak_usage",
        "tasks",
        "unschedulable",
    ]
    assert document["tasks"] == [
        {"task_id": "a", "start": 0, "end": 2, "units": {}},
        {"task_id": "b", "start": 2, "end": 3, "units": {"gpu": 1}},
    ]
    assert json.dumps(document, sort_keys=True) == json.dumps(plan.to_dict(), sort_keys=True)


def test_the_text_form() -> None:
    plan = build_schedule(
        build_graph([task("a", 2), task("b", 1, depends_on=("a",), demand={"gpu": 1})]),
        constraints(workers=1, gpu=1),
    )
    assert render_plan(plan) == ("plan scheduled makespan=3\n0000-0002 a -\n0002-0003 b gpu=1\n")


def test_the_text_form_lists_unschedulable_tasks() -> None:
    plan = build_schedule(
        build_graph([task("a", 1, demand={"workers": 3})]),
        constraints(workers=2),
    )
    assert render_plan(plan) == "plan partial makespan=0\nunschedulable a\n"


def test_two_builds_of_the_same_plan_are_identical() -> None:
    graph = build_graph([task("a", 1), task("b", 1), task("c", 1, depends_on=("a", "b"))])
    first = build_schedule(graph, constraints(workers=1)).to_dict()
    second = build_schedule(graph, constraints(workers=1)).to_dict()
    assert first == second


# -- the shipped sample ------------------------------------------------------


def test_the_sample_plan_schedules_completely() -> None:
    document = json.loads(SAMPLE.read_text(encoding="utf-8"))
    graph = build_graph(
        [
            TaskNode(
                task_id=raw["id"],
                duration=raw["duration"],
                depends_on=tuple(raw.get("depends_on") or ()),
                demand=raw.get("demand") or {},
            )
            for raw in document["tasks"]
        ]
    )
    pools = PoolSet(tuple(ResourcePool(raw["name"], raw["capacity"]) for raw in document["pools"]))
    calendar = Calendar(
        tuple(AvailabilityWindow(raw["start"], raw["end"]) for raw in document["windows"])
    )
    plan = build_schedule(graph, ResourceConstraints(pools=pools, calendar=calendar))

    assert plan.status is PlanStatus.SCHEDULED
    assert plan.unschedulable == ()
    assert [(item.task_id, item.start, item.end) for item in plan.tasks] == [
        ("build-a", 0, 2),
        ("build-b", 0, 2),
        ("test-a", 2, 4),
        ("test-b", 2, 3),
        ("package", 6, 8),
    ]
    assert plan.makespan == 8
    assert dict(plan.peak_usage) == {"gpu": 1, "workers": 2}
    assert plan.summary["usable_slots"] == 8
