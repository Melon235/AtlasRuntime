"""Offline checks for the Stage B benchmark protocol.

No model, no sandbox, no network. What is verified here is the design: the matrix is balanced and
reproducible, the arms are the frozen prompts, the tasks are new and solvable, every declared metric
can be read from a result record, and the reconstruction reproduces the two runs whose truth is
already known.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_DIR = REPO_ROOT / "experiments" / "stage_benchmark"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark import (  # noqa: E402
    estimate,
    matrix,
    protocol,
    reconstruct,
    schema,
)
from experiments.stage_benchmark.tasks import BY_ID, HISTORICAL_TASKS, SPECS, STRUCTURES  # noqa: E402


@pytest.fixture(scope="module")
def document() -> dict:
    return protocol.build_protocol()


# --------------------------------------------------------------------------- protocol identity


def test_the_protocol_validates(document: dict) -> None:
    assert protocol.validate(document) == []


def test_the_three_arms_are_the_frozen_prompts(document: dict) -> None:
    arms = {arm["arm_id"]: arm for arm in document["arms"]}
    assert sorted(arms) == ["P1", "P2", "P3"]
    assert arms["P1"]["observed_digest"] == (
        "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a"
    )
    assert arms["P2"]["observed_digest"] == (
        "sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323"
    )
    assert arms["P3"]["observed_digest"] == (
        "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489"
    )
    assert all(arm["matches_declaration"] for arm in document["arms"])


def test_each_arm_resolves_to_a_prompt_spec_with_its_own_bytes() -> None:
    specs = {arm: protocol.prompt_spec(arm) for arm in protocol.ARM_IDS}
    digests = {spec.digest for spec in specs.values()}
    assert len(digests) == 3
    assert specs["P1"].text != specs["P2"].text != specs["P3"].text


def test_the_run_configuration_is_the_frozen_one(document: dict) -> None:
    config = document["run_config"]
    assert config["model"] == "deepseek-chat"
    assert config["token_budget"] == 500_000
    assert config["main_max_turns"] == 20
    assert config["child_max_turns"] == 6
    assert config["max_concurrent_children"] == 2
    assert config["max_delegation_rounds"] == 3
    assert config["public_test_max_calls"] == 4
    assert config["tool_error_mode"] == "corrected"
    assert config["network"] == "none"
    assert config["repetitions"] == 2


# --------------------------------------------------------------------------- the matrix


def test_the_matrix_is_complete_and_paired(document: dict) -> None:
    cells = document["plan"]["cells"]
    assert len(cells) == 36
    pairs = {(cell["task_id"], cell["arm_id"], cell["repetition"]) for cell in cells}
    assert len(pairs) == 36
    for task_id in BY_ID:
        for arm_id in protocol.ARM_IDS:
            assert (
                sum(1 for cell in cells if cell["task_id"] == task_id and cell["arm_id"] == arm_id)
                == 2
            )


def test_every_round_holds_each_arm_equally(document: dict) -> None:
    cells = document["plan"]["cells"]
    rounds: dict[tuple[int, int], list[str]] = {}
    for cell in cells:
        rounds.setdefault((cell["repetition"], cell["round"]), []).append(cell["arm_id"])
    for arms in rounds.values():
        assert sorted(arms).count("P1") == 2
        assert sorted(arms).count("P2") == 2
        assert sorted(arms).count("P3") == 2


def test_no_arm_opens_both_repetitions(document: dict) -> None:
    first = {}
    for cell in document["plan"]["cells"]:
        first.setdefault(cell["repetition"], cell["arm_id"])
    assert first[1] != first[2]


def test_the_plan_is_reproducible_from_the_seed(document: dict) -> None:
    again = matrix.build_plan(
        list(BY_ID),
        list(protocol.ARM_IDS),
        repetitions=document["run_config"]["repetitions"],
        seed=document["run_config"]["seed"],
    )
    assert matrix.plan_digest(again) == document["plan"]["digest"]
    assert again == document["plan"]["cells"]


def test_a_different_seed_gives_a_different_order() -> None:
    first = matrix.build_plan(list(BY_ID), list(protocol.ARM_IDS), repetitions=2, seed=1)
    second = matrix.build_plan(list(BY_ID), list(protocol.ARM_IDS), repetitions=2, seed=2)
    assert matrix.plan_digest(first) != matrix.plan_digest(second)


# --------------------------------------------------------------------------- the tasks


def test_six_new_tasks_two_per_structure() -> None:
    assert len(SPECS) == 6
    for structure in STRUCTURES:
        assert sum(1 for spec in SPECS if spec.structure == structure) == 2, structure


def test_no_task_reuses_an_earlier_stage() -> None:
    assert not ({spec.task_id for spec in SPECS} & set(HISTORICAL_TASKS))


def test_every_task_declares_a_complete_contract() -> None:
    for spec in SPECS:
        assert spec.write_scope, spec.task_id
        assert len(spec.wrong) >= 3, spec.task_id
        assert spec.reference, spec.task_id
        assert set(spec.reference) <= set(spec.write_scope), spec.task_id
        # The marker belongs to the oracle, never to anything the agent can read.
        assert all("HIDDEN_OK" not in text for text in spec.files.values()), spec.task_id


def test_no_goal_names_the_experiment_or_the_workers() -> None:
    banned = ("delegate", "subworker", "benchmark", "oracle", "prompt", "worker")
    for spec in SPECS:
        lowered = spec.goal.lower()
        assert not [word for word in banned if word in lowered], spec.task_id
        assert "experiment" not in lowered, spec.task_id


def test_the_task_digests_are_stable(document: dict) -> None:
    recorded = {task["task_id"]: task["fixture_digest"] for task in document["tasks"]}
    rebuilt = protocol.build_protocol()
    assert recorded == {task["task_id"]: task["fixture_digest"] for task in rebuilt["tasks"]}


# --------------------------------------------------------------------------- metrics and schema


def test_every_declared_metric_has_a_resolver(document: dict) -> None:
    ids = [metric["id"] for metric in document["metrics"]]
    assert schema.unresolvable(ids) == []


def test_every_metric_declares_a_denominator_and_a_missing_policy(document: dict) -> None:
    for metric in document["metrics"]:
        assert metric["denominator"], metric["id"]
        assert metric["missing"], metric["id"]


def test_every_question_cites_a_declared_metric(document: dict) -> None:
    ids = {metric["id"] for metric in document["metrics"]}
    for question in document["questions"]:
        assert set(question["primary_metrics"]) <= ids, question["id"]


def test_the_schema_rejects_an_incomplete_record() -> None:
    record = {
        "analyzer": {"version": "x"},
        "run": {
            "run_id": "r",
            "task_id": "t",
            "arm_id": "P1",
            "repetition": 1,
            "status": "succeeded",
            "acceptance": "PASS",
            "stop_reason": "success",
        },
        "prompt": {"version": "v", "digest": "d"},
        "counts": {"provider_requests": 1, "tool_calls": 1},
        "consistency": {"gaps": []},
        "delegation": {"proposed": False, "children": 0},
        "context": {},
        "verification": {},
        "failure_class": "none",
    }
    problems = schema.validate_record(record)
    assert any("counts.assistant_turns" in item for item in problems)


# --------------------------------------------------------------------------- reconstruction


S1 = REPO_ROOT / "experiments/stage_s1_prompt_v2/evidence/run-1"
S2 = REPO_ROOT / "experiments/stage_s2_prompt_v21/evidence/run-1"


@pytest.mark.skipif(not S1.is_dir(), reason="stage S1 evidence is not present")
def test_the_s1_run_reconstructs_without_trusting_the_session() -> None:
    record = reconstruct.reconstruct(S1)
    assert record["counts"]["provider_requests"] == 18
    assert record["counts"]["tool_calls"] == 36
    assert record["counts"]["assistant_turns"] == 18
    assert record["counts"]["session_turns"] == 0
    assert record["counts"]["turns_delta"] == -18
    assert record["counts"]["turns_delta_reason"] is not None
    assert record["consistency"]["turns_explained"] is True
    assert record["delegation"]["first_delegation_turn"] == 17
    assert record["delegation"]["children"] == 1


@pytest.mark.skipif(not S2.is_dir(), reason="stage S2 evidence is not present")
def test_the_s2_run_reconstructs_its_one_turn_delta() -> None:
    record = reconstruct.reconstruct(S2)
    assert record["counts"]["provider_requests"] == 14
    assert record["counts"]["assistant_turns"] == 13
    assert record["counts"]["turns_delta"] == 1
    assert "text-only" in record["counts"]["turns_delta_reason"]
    assert record["counts"]["tool_calls"] == 24
    assert record["delegation"]["proposed"] is False
    assert record["verification"]["goal_passed"] is True
    assert record["verification"]["public_tests_passed"] is True


def test_a_budget_gap_is_explained_and_an_unknown_gap_is_not(tmp_path: Path) -> None:
    run = tmp_path / "run"
    run.mkdir()
    session = {
        "run_id": "r",
        "status": "failed",
        "acceptance": "FAIL",
        "main": {
            "task_id": "m",
            "turns": 0,
            "tool_calls": [],
            "stop_reason": "budget_exceeded",
            "output": "",
        },
        "metrics": {"main_requests": 3, "system_total_tokens": 10},
    }
    conversations = {
        "tasks": {
            "m": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "name": "file_read", "input": {}, "id": "1"}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "content": "ok", "is_error": False}],
                    },
                ]
            }
        }
    }
    (run / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (run / "tool_conversations.json").write_text(json.dumps(conversations), encoding="utf-8")
    record = reconstruct.reconstruct(run)
    assert record["consistency"]["turns_explained"] is True
    assert record["consistency"]["explained"]
    assert not [gap for gap in record["consistency"]["gaps"] if "session reports" in gap]

    session["main"]["stop_reason"] = "success"
    (run / "session.json").write_text(json.dumps(session), encoding="utf-8")
    record = reconstruct.reconstruct(run)
    assert record["consistency"]["turns_explained"] is False


# --------------------------------------------------------------------------- aggregation


def _record(arm: str, *, oracle: str, stop: str, proposed: bool, peak: int, total: int) -> dict:
    """A result record in the 1.1 shape. The delegation block is what the ladder reads."""

    admitted = 1 if proposed else 0
    completed = proposed and oracle == "PASS"
    levels = {
        "L1_proposal": proposed,
        "L2_admission": bool(admitted),
        "L3_worker_completion": bool(admitted),
        "L4_handoff_delivery": bool(admitted),
        "L5_main_continuation": bool(admitted),
        "L6_goal_correctness": oracle == "PASS",
    }
    reached = 0
    for name in (
        "L1_proposal",
        "L2_admission",
        "L3_worker_completion",
        "L4_handoff_delivery",
        "L5_main_continuation",
        "L6_goal_correctness",
    ):
        if not levels[name]:
            break
        reached += 1
    return {
        "run": {
            "run_id": f"{arm}-r",
            "task_id": "slugkit",
            "arm_id": arm,
            "repetition": 1,
            "status": "succeeded",
            "acceptance": "PASS" if oracle == "PASS" else "FAIL",
            "stop_reason": stop,
        },
        "analyzer": {"version": reconstruct.ANALYZER_VERSION},
        "prompt": {"version": "main-v1.0", "digest": "sha256:0"},
        "counts": {"provider_requests": 10, "assistant_turns": 10, "tool_calls": 10},
        "consistency": {"gaps": []},
        "delegation": {
            "proposed": proposed,
            "proposals": 1 if proposed else 0,
            "children": admitted,
            "children_admitted": admitted,
            "children_succeeded": admitted,
            "first_delegation_turn": 3 if proposed else None,
            "first_implementation_turn": 5,
            "routes": ["delegate_one"] if proposed else [],
            "admission": {
                "proposals": 1 if proposed else 0,
                "admitted": admitted,
                "rejected": 0,
                "unknown": 0,
                "state": "ADMITTED" if admitted else "NO_PROPOSAL",
            },
            "levels": {"met": levels, "level_reached": reached},
        },
        "handoff": {
            "delivered": bool(admitted),
            "usable": bool(completed),
            "payload_chars": 100 if admitted else 0,
            "overlap_files": None,
        },
        "capability": {"invalid": False if proposed else None, "basis": "synthetic"},
        "failure": {
            "primary": "none" if oracle == "PASS" else "goal",
            "contributing": [],
            "confidence": "HIGH",
        },
        "usage": {
            "main_prompt_hit_tokens": 0,
            "main_prompt_miss_tokens": 0,
            "child_prompt_tokens": 0,
            "main_completion_tokens": 100,
            "child_completion_tokens": 0,
        },
        "context": {
            "main_peak_context_tokens": peak,
            "system_total_tokens": total,
            "main_cumulative_input_tokens": total - 100,
            "main_cache_hit_tokens": None,
            "main_cache_miss_tokens": None,
            "child_total_prompt_tokens": 0,
            "completion_tokens": 100,
            "wall_clock_seconds": 60.0,
            "handoff_payload_chars": 0,
        },
        "verification": {
            "public_test_calls": 1,
            "public_tests_passed": True,
            "post_failure_edits": 0,
            "changed_files": 1,
            "ownership_denials": 0,
            "oracle": oracle,
            "goal_passed": oracle == "PASS",
        },
        "failure_class": "none" if oracle == "PASS" else "goal",
    }


def test_aggregation_counts_every_attempt_in_the_denominator() -> None:
    records = [
        _record("P1", oracle="PASS", stop="success", proposed=False, peak=100, total=1000),
        _record("P1", oracle="FAIL", stop="budget_exceeded", proposed=True, peak=200, total=2000),
        _record("P1", oracle="FAIL", stop="success", proposed=False, peak=150, total=1500),
    ]
    summary = reconstruct.aggregate(records)
    arm = summary["by_arm"]["P1"]
    assert summary["attempts"] == 3
    assert arm["oracle_pass_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert arm["budget_exhaustion_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert arm["delegation_proposal_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert arm["direct_execution_rate"] == pytest.approx(2 / 3, abs=1e-4)
    # 1.1 moved effectiveness onto its own denominator: one proposal, admitted, worker completed,
    # handoff usable, and the run failed its oracle, so the mechanism completed (L5) but the goal
    # (L6) did not. Both rates are over proposals, and both denominators are recorded.
    assert arm["delegation_mechanism_completion_rate"] == pytest.approx(1.0, abs=1e-4)
    assert arm["effective_delegation_rate"] == pytest.approx(0.0, abs=1e-4)
    assert arm["levels"]["L1_proposal"]["denominator"] == 3
    assert arm["levels"]["L2_admission"]["denominator"] == 1


def test_aggregation_pairs_observations_by_task_and_repetition() -> None:
    records = [
        _record("P1", oracle="PASS", stop="success", proposed=False, peak=100, total=1000),
        _record("P3", oracle="PASS", stop="success", proposed=True, peak=80, total=1200),
    ]
    summary = reconstruct.aggregate(records)
    assert len(summary["paired"]) == 1
    pair = summary["paired"][0]
    assert pair["task_id"] == "slugkit"
    assert pair["peak_context"] == {"P1": 100, "P3": 80}
    assert pair["delegated"] == {"P1": False, "P3": True}


# --------------------------------------------------------------------------- cost


def test_the_estimate_is_monotone_and_priced_from_the_frozen_config() -> None:
    payload = estimate.from_repository(cells=36, budget_tokens=500_000)
    low = payload["scenarios"]["LOW"]
    expected = payload["scenarios"]["EXPECTED"]
    high = payload["scenarios"]["HIGH"]
    assert low["total_tokens"] < expected["total_tokens"] < high["total_tokens"]
    assert low["total_cost_usd"] < expected["total_cost_usd"] < high["total_cost_usd"]
    assert payload["pricing"]["cache_hit_input_per_million"] == 0.006
    assert payload["pricing"]["snapshot_date"]
    assert payload["cells"] == 36


def test_the_estimate_scales_with_the_number_of_cells() -> None:
    twelve = estimate.from_repository(cells=12, budget_tokens=500_000)
    thirty_six = estimate.from_repository(cells=36, budget_tokens=500_000)
    assert thirty_six["scenarios"]["EXPECTED"]["total_tokens"] == pytest.approx(
        3 * twelve["scenarios"]["EXPECTED"]["total_tokens"], rel=1e-6
    )


# --------------------------------------------------------------------------- freeze and runner


def _runner():  # noqa: ANN202 - a module handle, imported by path
    sys.path.insert(0, str(STAGE_DIR))
    import run_stage_b  # noqa: PLC0415  # pyright: ignore[reportMissingImports]

    return run_stage_b


def test_the_frozen_runtime_is_the_one_the_last_stage_froze() -> None:
    run_stage_b = _runner()
    identity = run_stage_b.source_identity()
    assert identity["tree_digest"] == run_stage_b.S2_SOURCE_TREE_DIGEST
    assert identity["file_count"] == 130


def test_the_runner_has_no_live_command() -> None:
    help_text = subprocess.run(
        [sys.executable, str(STAGE_DIR / "run_stage_b.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    assert "--run" not in help_text
    refused = subprocess.run(
        [sys.executable, str(STAGE_DIR / "run_stage_b.py"), "--run"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert refused.returncode != 0


@pytest.mark.skipif(not (STAGE_DIR / "offline.json").is_file(), reason="offline report absent")
def test_the_recorded_offline_report_passed() -> None:
    report = json.loads((STAGE_DIR / "offline.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["wrong_variants"] == report["rejected_variants"]
    assert set(report["tasks"]) == set(BY_ID)


@pytest.mark.skipif(
    not (STAGE_DIR / "freeze.json").is_file(), reason="the stage has not been frozen yet"
)
def test_the_freeze_records_what_the_protocol_says() -> None:
    frozen = json.loads((STAGE_DIR / "freeze.json").read_text(encoding="utf-8"))
    document = protocol.read_protocol()
    assert frozen["plan"]["digest"] == document["plan"]["digest"]
    assert frozen["runtime"]["tree_digest"] == run_stage_b_digest()
    assert frozen["prompts"]["P3"]["digest"] == (
        "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489"
    )
    assert frozen["analysis"]["schema_version"] == schema.SCHEMA_VERSION


def run_stage_b_digest() -> str:
    return _runner().source_identity()["tree_digest"]
