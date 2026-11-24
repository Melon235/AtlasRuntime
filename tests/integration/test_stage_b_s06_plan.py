"""Stage B S06: the prepared 36-cell plan must agree with the CLI it describes.

The plan and the authorization package are generated JSON, so they can silently drift from the
runner they claim to drive. These tests re-derive every path from ``admission``/``provision`` and
compare, and they assert the package stays fail-closed until freeze 1.6 exists. Nothing is executed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark import protocol  # noqa: E402
from experiments.stage_benchmark.execution import admission, provision  # noqa: E402

BENCHMARK = REPO_ROOT / "experiments" / "stage_benchmark"
PLAN_PATH = BENCHMARK / "s06-36cell-plan.json"
PACKAGE_PATH = BENCHMARK / "s06-authorization-package.json"


def _plan() -> dict:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def _package() -> dict:
    return json.loads(PACKAGE_PATH.read_text(encoding="utf-8"))


def test_the_plan_covers_the_frozen_matrix_exactly() -> None:
    plan = _plan()
    assert plan["matrix"]["cells"] == 36
    assert plan["matrix"]["shape_ok"] is True
    assert plan["matrix"]["cells_by_arm"] == {"P1": 12, "P2": 12, "P3": 12}
    cells = plan["cells"]
    assert len(cells) == 36
    assert [cell["order"] for cell in cells] == list(range(1, 37))
    assert len({cell["cell_id"] for cell in cells}) == 36
    frozen_plan = json.loads(protocol.PLAN_PATH.read_text(encoding="utf-8"))
    assert {item["cell_id"] for item in frozen_plan["plan"]["cells"]} == {
        cell["cell_id"] for cell in cells
    }


def test_every_planned_path_is_the_path_the_runner_would_use() -> None:
    plan = _plan()
    document = protocol.read_protocol()
    evidence_root = REPO_ROOT / plan["execution"]["evidence_root"]
    for cell in plan["cells"]:
        identity = provision.cell_from_plan(document, cell["cell_id"])
        cell_root = admission.cell_dir(evidence_root, identity)
        assert str(cell_root.relative_to(REPO_ROOT)).replace("\\", "/") == cell["evidence_dir"]
        assert (
            str(admission.attempt_path(cell_root).relative_to(REPO_ROOT)).replace("\\", "/")
            == cell["claim_path"]
        )
        assert cell["attempts_allowed"] == 1
        assert cell["retry"] == "FORBIDDEN"


def test_the_sandbox_resources_use_the_quantity_the_audit_compares() -> None:
    plan = _plan()
    config = protocol.read_protocol()["run_config"]
    assert plan["config"]["cpu"] == str(config["cpu"]) == "1"
    assert plan["config"]["memory"] == provision.memory_quantity(config["memory_gib"]) == "1Gi"
    assert plan["config"]["network"] == config["network"] == "none"
    assert plan["config"]["image"] == config["image"]


def test_the_plan_names_the_host_executor_as_the_scheduler_gate() -> None:
    plan = _plan()
    assert plan["execution"]["scheduler_gate"] == "PROVIDED_BY_HOST_EXECUTOR"
    assert plan["execution"]["reference_policy"].endswith("matrix_executor.py")
    assert "claim hygiene" in plan["execution"]["scheduler_note"]
    assert "MEASURED_AGENT_FAILURE_CONTINUE" in plan["execution"]["global_stop_loss"]
    assert plan["execution"]["concurrency"] == 1
    assert plan["execution"]["per_cell_attempts"] == 1


def test_the_package_is_a_request_that_the_gate_still_refuses() -> None:
    package = _package()
    plan = _plan()
    freeze = admission.read_json(admission.FREEZE_PATH)
    assert package["status"] == "PENDING_OWNER_APPROVAL"
    # The digests are final now, but a record is not an approval.
    assert package["approved_digests"]["runner_source"] == admission.source_identity()["digest"]
    assert package["approved_digests"]["runner_source"] == freeze["source"]["digest"]
    assert package["freeze_binding"]["version"] == "stage-b-execution/1.6"
    assert package["freeze_binding"]["written"] is True
    assert package["freeze_binding"]["raw_sha256"] == admission.digest_bytes(
        admission.FREEZE_PATH.read_bytes()
    )
    assert package["cost_ceiling_usd"] is None
    assert package["consumed"] is False
    assert set(package["cells"]) == {cell["cell_id"] for cell in plan["cells"]}
    assert package["contract"]["resources"] == {"cpu": "1", "memory": "1Gi"}

    preflight = {
        "protocol_digest": package["approved_digests"]["protocol"],
        "plan_digest": package["approved_digests"]["plan"],
        "runtime_digest": package["approved_digests"]["runtime"],
        "runner_source_digest": package["approved_digests"]["runner_source"],
    }
    gate = admission.authorization_gate(
        package,
        cell=provision.cell_from_plan(protocol.read_protocol(), plan["cells"][0]["cell_id"]),
        preflight_result=preflight,
        live=True,
    )
    assert gate["ready"] is False
    assert gate["problems"] == [
        "status='PENDING_OWNER_APPROVAL'",
        "missing_approved_by",
        "missing_approval_commit",
        "missing_approved_at",
        "no_cost_ceiling",
    ]


def test_a_missing_active_freeze_blocks_a_live_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolated in tmp_path: after 1.6 exists the real tree has no missing active freeze."""

    assert admission.FREEZE_VERSION == "stage-b-execution/1.6"
    monkeypatch.setattr(admission, "FREEZE_PATH", tmp_path / "freeze-1.6.json")
    live = admission.preflight(
        evidence_root=REPO_ROOT / "experiments/stage_b_benchmark/cells", live=True
    )
    assert live["status"] == "BLOCKED"
    labels = {item["label"]: item["ok"] for item in live["checks"]}
    assert labels["active_execution_freeze_present"] is False
