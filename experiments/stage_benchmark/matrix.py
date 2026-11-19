"""The experiment matrix: which prompt runs on which task, in which order.

The design is a Latin rectangle, not a shuffle:

* inside one repetition each task is run once per arm, so the pairing is complete;
* inside one *round* each arm appears the same number of times, so no arm is systematically earlier;
* a task's arm order rotates with its position and with the repetition, so the same arm is not
  always first for the same task, and the first cell is not the same arm in both repetitions.

Everything is derived from the seed alone, so the order can be regenerated and checked.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from typing import Any

DEFAULT_SEED = 20261119


def build_plan(
    task_ids: Sequence[str],
    arm_ids: Sequence[str],
    *,
    repetitions: int,
    seed: int = DEFAULT_SEED,
) -> list[dict[str, Any]]:
    """Every cell of the benchmark, in execution order."""

    tasks = list(task_ids)
    arms = list(arm_ids)
    if not tasks or not arms:
        raise ValueError("a plan needs at least one task and one arm")
    if len(set(tasks)) != len(tasks) or len(set(arms)) != len(arms):
        raise ValueError("task and arm ids must be unique")
    if repetitions < 1:
        raise ValueError("repetitions must be at least 1")

    cells: list[dict[str, Any]] = []
    order = 0
    for repetition in range(1, repetitions + 1):
        order_rng = random.Random(seed + repetition)
        round_tasks = list(tasks)
        order_rng.shuffle(round_tasks)
        for round_index in range(len(arms)):
            for position, task_id in enumerate(round_tasks):
                arm_id = arms[(position + round_index + repetition - 1) % len(arms)]
                order += 1
                cells.append(
                    {
                        "cell_id": f"{task_id}.{arm_id}.r{repetition}",
                        "task_id": task_id,
                        "arm_id": arm_id,
                        "repetition": repetition,
                        "round": round_index + 1,
                        "order": order,
                    }
                )
    return cells


def plan_digest(cells: Sequence[dict[str, Any]]) -> str:
    payload = json.dumps(list(cells), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def balance_report(cells: Sequence[dict[str, Any]], arm_ids: Sequence[str]) -> dict[str, Any]:
    """The properties the design promises, measured on the plan itself."""

    by_round: dict[tuple[int, int], dict[str, int]] = {}
    first_arm: dict[int, str] = {}
    for cell in cells:
        key = (cell["repetition"], cell["round"])
        bucket = by_round.setdefault(key, {})
        bucket[cell["arm_id"]] = bucket.get(cell["arm_id"], 0) + 1
        if cell["repetition"] not in first_arm:
            first_arm[cell["repetition"]] = cell["arm_id"]

    pairs = [(cell["task_id"], cell["arm_id"], cell["repetition"]) for cell in cells]
    expected_pairs = len({task for task, _, _ in pairs}) * len(set(arm_ids))
    per_round_balanced = all(
        set(bucket) == set(arm_ids) and len(set(bucket.values())) == 1
        for bucket in by_round.values()
    )
    return {
        "cells": len(cells),
        "unique_pairs": len(set(pairs)) == len(pairs),
        "complete_pairs": len({(task, arm) for task, arm, _ in pairs}) == expected_pairs,
        "each_arm_in_each_round": per_round_balanced,
        "first_arm_by_repetition": first_arm,
        "first_arm_differs_across_repetitions": len(set(first_arm.values())) == len(first_arm)
        if len(first_arm) > 1
        else True,
    }
