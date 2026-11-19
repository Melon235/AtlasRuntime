"""Hidden acceptance program for the WorkGrid fixture.

Runs inside the sandbox, after the agent has stopped, from the repository root. It imports only the
fixture's public surface and asserts behaviour the public suite does not spell out: precedence,
propagation, determinism across build orders, input immutability and the canonical forms.

It prints ``HIDDEN_OK`` and exits 0 when every check passes, otherwise one ``FAIL <message>`` line
per failed check plus ``HIDDEN_FAIL`` and exit 1.
"""

from __future__ import annotations

import json
import pathlib
import traceback

FAILURES: list[str] = []


def check(condition: object, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def task(task_id: str, duration: int = 1, depends_on=(), demand=None):
    from workgrid.dependencies.graph import TaskNode

    return TaskNode(
        task_id=task_id,
        duration=duration,
        depends_on=tuple(depends_on),
        demand=demand or {},
    )


def constraints(workers: int = 1, gpu=None, windows=((0, 10),)):
    from workgrid.resources import (
        AvailabilityWindow,
        Calendar,
        PoolSet,
        ResourceConstraints,
        ResourcePool,
    )

    pools = [ResourcePool("workers", workers)]
    if gpu is not None:
        pools.append(ResourcePool("gpu", gpu))
    calendar = Calendar(tuple(AvailabilityWindow(start, end) for start, end in windows))
    return ResourceConstraints(pools=PoolSet(tuple(pools)), calendar=calendar)


def run(tasks, **kwargs):
    from workgrid.dependencies import build_graph
    from workgrid.scheduler import build_schedule

    return build_schedule(build_graph(tasks), constraints(**kwargs))


def placed(plan):
    return [(item.task_id, item.start, item.end) for item in plan.tasks]


def reasons(plan):
    return [str(item.reason) for item in plan.unschedulable]


def main() -> int:
    from workgrid.dependencies import CycleDetectedError, UnknownDependencyError
    from workgrid.scheduler import PlanStatus, build_schedule, render_plan

    # -- 1. the empty plan ---------------------------------------------------
    empty = run([])
    check(empty.status is PlanStatus.EMPTY, "an empty plan must be EMPTY")
    check(empty.makespan == 0, "an empty plan has makespan 0")
    check(empty.tasks == () and empty.unschedulable == (), "an empty plan has no entries")
    check(render_plan(empty) == "plan empty makespan=0\n", "empty text form")

    # -- 2. a chain, and the summary ----------------------------------------
    chain = run(
        [
            task("a", 2, demand={"workers": 1}),
            task("b", 3, depends_on=("a",), demand={"workers": 1}),
        ],
        workers=1,
    )
    check(placed(chain) == [("a", 0, 2), ("b", 2, 5)], f"chain placement {placed(chain)}")
    check(chain.makespan == 5, "chain makespan")
    check(
        dict(chain.summary)
        == {"makespan": 5, "scheduled": 2, "tasks": 2, "unschedulable": 0, "usable_slots": 10},
        f"chain summary {dict(chain.summary)}",
    )

    # -- 2b. a dependency delays work even when capacity is free -----------
    forced = run([task("a", 2), task("b", 1, depends_on=("a",))], workers=2)
    check(placed(forced) == [("a", 0, 2), ("b", 2, 3)], f"dependency delay {placed(forced)}")

    # -- 3. capacity decides whether two tasks overlap ----------------------
    parallel = run(
        [task("a", 2, demand={"workers": 1}), task("b", 2, demand={"workers": 1})], workers=2
    )
    check(placed(parallel) == [("a", 0, 2), ("b", 0, 2)], f"parallel placement {placed(parallel)}")
    check(dict(parallel.peak_usage) == {"workers": 2}, "parallel peak usage")

    sequential = run(
        [task("a", 2, demand={"workers": 1}), task("b", 2, demand={"workers": 1})], workers=1
    )
    check(
        placed(sequential) == [("a", 0, 2), ("b", 2, 4)],
        f"sequential placement {placed(sequential)}",
    )
    check(dict(sequential.peak_usage) == {"workers": 1}, "sequential peak usage")

    # -- 4. a scarce resource serializes only itself ------------------------
    scarce = run(
        [
            task("a", 1, demand={"workers": 1, "gpu": 1}),
            task("b", 1, demand={"workers": 1, "gpu": 1}),
            task("c", 1, demand={"workers": 1}),
        ],
        workers=2,
        gpu=1,
    )
    check(
        placed(scarce) == [("a", 0, 1), ("c", 0, 1), ("b", 1, 2)],
        f"scarce resource placement {placed(scarce)}",
    )
    check(dict(scarce.peak_usage) == {"gpu": 1, "workers": 2}, "scarce peak usage")

    # -- 5. a crowded plan uses the last slot the window allows -------------
    packed = run(
        [task("a", 6, demand={"workers": 1}), task("b", 4, demand={"workers": 1})],
        workers=1,
        windows=((0, 10),),
    )
    check(placed(packed) == [("a", 0, 6), ("b", 6, 10)], f"packed placement {placed(packed)}")
    check(packed.makespan == 10, "packed makespan")

    # -- 6. windows are respected, gaps included ----------------------------
    gapped = run([task("a", 3)], workers=1, windows=((0, 2), (4, 10)))
    check(placed(gapped) == [("a", 4, 7)], f"gapped placement {placed(gapped)}")

    too_long = run([task("a", 5)], workers=1, windows=((0, 4),))
    check(too_long.status is PlanStatus.PARTIAL, "a task that never fits makes the plan partial")
    check(reasons(too_long) == ["no_available_window"], f"too long reason {reasons(too_long)}")
    check(
        render_plan(too_long) == "plan partial makespan=0\nunschedulable a\n",
        "unschedulable text form",
    )

    # -- 7. classification and precedence -----------------------------------
    over = run([task("a", 1, demand={"workers": 3}), task("b", 1, depends_on=("a",))], workers=2)
    check(over.unschedulable_ids() == ("a", "b"), f"over capacity ids {over.unschedulable_ids()}")
    check(
        reasons(over) == ["demand_exceeds_capacity", "dependency_unschedulable"],
        f"over capacity reasons {reasons(over)}",
    )
    check(over.scheduled_ids() == (), "nothing is scheduled when everything is blocked")

    unknown = run([task("a", 1, demand={"memory": 1})], workers=1)
    check(reasons(unknown) == ["unknown_resource"], f"unknown resource reason {reasons(unknown)}")

    # precedence: a blocked dependency wins over the task's own impossible demand
    precedence = run(
        [
            task("a", 1, demand={"workers": 9}),
            task("b", 1, depends_on=("a",), demand={"workers": 9}),
        ],
        workers=1,
    )
    check(
        reasons(precedence) == ["demand_exceeds_capacity", "dependency_unschedulable"],
        f"precedence reasons {reasons(precedence)}",
    )

    # a blocked task blocks its whole chain, not only its direct dependents
    cascade = run(
        [
            task("a", 1, demand={"workers": 9}),
            task("b", 1, depends_on=("a",)),
            task("c", 1, depends_on=("b",)),
        ],
        workers=1,
    )
    check(
        cascade.unschedulable_ids() == ("a", "b", "c"), f"cascade ids {cascade.unschedulable_ids()}"
    )
    check(
        reasons(cascade)
        == ["demand_exceeds_capacity", "dependency_unschedulable", "dependency_unschedulable"],
        f"cascade reasons {reasons(cascade)}",
    )

    # -- 8. broken graphs stay exceptions -----------------------------------
    cyclic = [
        task("a", 1, depends_on=("c",)),
        task("b", 1, depends_on=("a",)),
        task("c", 1, depends_on=("b",)),
    ]
    try:
        run(cyclic)
        check(False, "a cycle was turned into a plan instead of an exception")
    except CycleDetectedError:
        pass
    except Exception as error:  # noqa: BLE001 - the point is the type
        check(False, f"a cycle raised {type(error).__name__}")

    dangling = [task("a", 1, depends_on=("ghost",))]
    try:
        run(dangling)
        check(False, "an unknown dependency was turned into a plan")
    except UnknownDependencyError:
        pass
    except Exception as error:  # noqa: BLE001
        check(False, f"an unknown dependency raised {type(error).__name__}")

    # -- 9. determinism, canonical form, immutability ------------------------
    tasks = [
        task("b", 1, depends_on=("a",), demand={"workers": 1}),
        task("a", 2, demand={"workers": 1}),
        task("c", 1),
    ]
    from workgrid.dependencies import build_graph

    first_graph = build_graph(tasks)
    before_nodes = dict(first_graph.nodes)
    first = build_schedule(first_graph, constraints(workers=1))
    second = build_schedule(build_graph(list(reversed(tasks))), constraints(workers=1))
    check(first.to_dict() == second.to_dict(), "the plan depends on task declaration order")
    check(dict(first_graph.nodes) == before_nodes, "the graph was mutated by scheduling")
    check(
        build_schedule(first_graph, constraints(workers=1)).to_dict() == first.to_dict(),
        "two runs of one graph disagree",
    )

    document = first.to_dict()
    check(
        list(document) == ["status", "makespan", "summary", "peak_usage", "tasks", "unschedulable"],
        f"canonical key order {list(document)}",
    )
    check(
        json.dumps(document, sort_keys=True) == json.dumps(first.to_dict(), sort_keys=True),
        "the dict form is not stable",
    )
    check(
        [item["task_id"] for item in document["tasks"]] == ["a", "c", "b"],
        f"canonical task order {[item['task_id'] for item in document['tasks']]}",
    )
    check(
        all(list(item["units"]) == sorted(item["units"]) for item in document["tasks"]),
        "units are not sorted in the dict form",
    )

    text = render_plan(first)
    check(text.endswith("\n") and not text.endswith("\n\n"), "text form must end with one newline")
    check(text.splitlines()[0] == f"plan scheduled makespan={first.makespan}", "text header")

    # -- 10. the shipped sample ---------------------------------------------
    sample = pathlib.Path("samples/plan.json")
    document = json.loads(sample.read_text(encoding="utf-8"))
    from workgrid.resources import (
        AvailabilityWindow,
        Calendar,
        PoolSet,
        ResourceConstraints,
        ResourcePool,
    )

    graph = build_graph(
        [
            task(
                raw["id"],
                raw["duration"],
                raw.get("depends_on") or (),
                raw.get("demand") or {},
            )
            for raw in document["tasks"]
        ]
    )
    pools = PoolSet(tuple(ResourcePool(raw["name"], raw["capacity"]) for raw in document["pools"]))
    calendar = Calendar(
        tuple(AvailabilityWindow(raw["start"], raw["end"]) for raw in document["windows"])
    )
    sample_plan = build_schedule(graph, ResourceConstraints(pools=pools, calendar=calendar))
    check(sample_plan.status is PlanStatus.SCHEDULED, f"sample status {str(sample_plan.status)}")
    check(
        placed(sample_plan)
        == [
            ("build-a", 0, 2),
            ("build-b", 0, 2),
            ("test-a", 2, 4),
            ("test-b", 2, 3),
            ("package", 6, 8),
        ],
        f"sample placement {placed(sample_plan)}",
    )
    check(sample_plan.makespan == 8, "sample makespan")
    check(dict(sample_plan.peak_usage) == {"gpu": 1, "workers": 2}, "sample peak usage")
    check(
        dict(sample_plan.summary)
        == {
            "makespan": 8,
            "scheduled": 5,
            "tasks": 5,
            "unschedulable": 0,
            "usable_slots": 8,
        },
        f"sample summary {dict(sample_plan.summary)}",
    )

    if FAILURES:
        for failure in FAILURES:
            print(f"FAIL {failure}")
        print("HIDDEN_FAIL")
        return 1
    print("HIDDEN_OK")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - any crash is a failed acceptance
        traceback.print_exc()
        print("HIDDEN_FAIL")
        raise SystemExit(1) from None
