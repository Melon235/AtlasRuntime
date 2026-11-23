"""Stage B-E2A: offline verification of the per-cell execution layer.

Every test is synthetic or uses the repository's scripted provider. No test calls DeepSeek, starts a
real sandbox, runs a calibration, or executes a scored benchmark cell.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from experiments.stage_benchmark import protocol, reconstruct, schema  # noqa: E402
from experiments.stage_benchmark.execution import (  # noqa: E402
    admission,
    collector,
    network,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import (  # noqa: E402
    AttemptAlreadyStarted,
    CellRequest,
    ExecutionBlocked,
)
from experiments.stage_benchmark.tasks import BY_ID  # noqa: E402
from tests.helpers.fake_sandbox_provider import FakeSandboxProvider  # noqa: E402
from tests.helpers.stage_p import PipelineRig, text, tool_call  # noqa: E402

from atlasos.app.main_driven import run_goal  # noqa: E402


def _document() -> dict[str, Any]:
    return protocol.read_protocol()


def _cell(cell_id: str = "slugkit.P1.r1") -> Any:
    return provision.cell_from_plan(_document(), cell_id)


# --------------------------------------------------------------------------- identity


def test_cell_identity_is_the_full_frozen_tuple() -> None:
    cell = _cell("zonelint.P1.r1")
    assert (cell.task_id, cell.arm_id, cell.repetition, cell.cell_id) == (
        "zonelint",
        "P1",
        1,
        "zonelint.P1.r1",
    )
    assert cell.in_matrix is True
    with pytest.raises(ValueError, match="not in the frozen"):
        provision.cell_from_plan(_document(), "zonelint.P1.r9")


def test_all_36_cells_rederive_the_frozen_identities() -> None:
    document = _document()
    for entry in document["plan"]["cells"]:
        cell = provision.cell_from_plan(document, entry["cell_id"])
        assert (
            provision.verify_identity(
                cell,
                document=document,
                runtime_digest="sha256:4eebcf1e56e7bf786320fda4252b2c584e040a702ae8642b4336cf21432d6dae",
                plan_digest="sha256:c69144744b14745677fdad9d9cfce69cdc9f777499aa7dabcc804e4b6fe76648",
            )
            == []
        )


def test_no_prompt_body_is_hardcoded_in_the_execution_layer() -> None:
    texts = [protocol.arm_text(arm) for arm in protocol.ARM_IDS]
    for path in admission.EXECUTION_DIR.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for body in texts:
            assert body[:120] not in source, path.name
    for arm in protocol.ARM_IDS:
        spec = protocol.prompt_spec(arm)
        declared = provision.arm_entry(_document(), arm)
        assert spec.digest == declared["declared_digest"]
        assert spec.version == declared["label"]


def test_options_carry_the_frozen_arm_and_run_config(tmp_path: Path) -> None:
    document = _document()
    fixture = provision.materialize_fixture(tmp_path / "fixture", BY_ID["slugkit"])
    for arm in protocol.ARM_IDS:
        cell = provision.cell_from_plan(document, f"slugkit.{arm}.r1")
        options = provision.build_options(
            cell=cell,
            fixture_repo=fixture,
            runs_root=tmp_path / "runs",
            run_id=f"probe-{arm}",
            document=document,
            provider_factory=offline.FakeProvider().build,
            sandbox_factory=lambda _root: offline.FakeProvider(),
            offline=True,
        )
        assert options.main_prompt is not None
        assert options.sandbox is not None
        assert options.main_prompt.digest == provision.arm_entry(document, arm)["declared_digest"]
        assert options.model == document["run_config"]["model"]
        assert options.max_turns == document["run_config"]["main_max_turns"]
        assert options.token_budget == document["run_config"]["token_budget"]
        assert options.delegation.child_max_turns == document["run_config"]["child_max_turns"]
        assert options.delegation.max_parallel_children == 2
        assert options.sandbox.image == document["run_config"]["image"]
        assert options.sandbox.provider == "fake"
        assert options.sandbox.require_real is False


# --------------------------------------------------------------------------- provisioning


def test_fixture_is_fresh_committed_and_never_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    created = provision.materialize_fixture(root, BY_ID["slugkit"])
    assert created == root
    assert (root / ".git").is_dir()
    assert (root / "slugkit" / "core.py").is_file()
    with pytest.raises(RuntimeError, match="already exists"):
        provision.materialize_fixture(root, BY_ID["slugkit"])


def test_oracle_is_host_only_and_never_in_the_agent_surface() -> None:
    document = _document()
    result = provision.isolation_checks(document)
    assert result["ok"] is True, result["problems"]
    assert result["executed_after_main_stops"] is True
    for spec in BY_ID.values():
        visible = "\n".join([spec.goal, *spec.files.values()])
        assert spec.oracle_source not in visible
        assert "HIDDEN_OK" not in visible


def test_calibration_is_outside_the_matrix_and_composes_real_options(tmp_path: Path) -> None:
    document = _document()
    cell = provision.calibration_cell()
    assert cell.in_matrix is False
    assert cell.cell_id not in {item["cell_id"] for item in document["plan"]["cells"]}
    fixture = provision.materialize_calibration_fixture(tmp_path / "calibration")
    options = provision.build_options(
        cell=cell,
        fixture_repo=fixture,
        runs_root=tmp_path / "runs",
        run_id="calibration-offline",
        document=document,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
        offline=True,
    )
    assert options.main_prompt is not None
    assert options.main_prompt.digest == provision.arm_entry(document, "P1")["declared_digest"]
    assert options.goal != ""
    assert (fixture / ".git").is_dir()


# --------------------------------------------------------------------------- admission


def test_preflight_rederives_every_frozen_identity(tmp_path: Path) -> None:
    result = admission.preflight(evidence_root=tmp_path / "evidence", require_offline_report=False)
    assert result["status"] == "READY", result["problems"]
    assert result["protocol_version"] == "1.1.0"
    assert result["plan_digest"].startswith("sha256:c6914474")
    assert result["runtime_digest"].startswith("sha256:4eebcf1e")
    assert {item["label"] for item in result["checks"]} >= {
        "host_oracle_isolated",
        "prompt_versions_and_digests_unchanged",
        "task_and_oracle_digests_unchanged",
        "run_config_unchanged",
    }


def test_preflight_blocks_a_cell_with_an_existing_marker(tmp_path: Path) -> None:
    cell = _cell()
    root = tmp_path / "evidence"
    directory = admission.cell_dir(root, cell)
    admission.claim_attempt(
        directory, cell=cell, preflight_digest="p", authorization_digest="a", source_digest="s"
    )
    result = admission.preflight(evidence_root=root, cell=cell, require_offline_report=False)
    assert result["status"] == "BLOCKED"
    assert "no_cell_marker_or_evidence" in " ".join(result["problems"])


def test_attempt_claim_is_atomic_under_a_thread_race(tmp_path: Path) -> None:
    cell = _cell("cascade.P3.r2")
    directory = admission.cell_dir(tmp_path, cell)
    outcomes: list[str] = []

    def claim() -> None:
        try:
            admission.claim_attempt(
                directory,
                cell=cell,
                preflight_digest="p",
                authorization_digest="a",
                source_digest="s",
            )
            outcomes.append("won")
        except AttemptAlreadyStarted:
            outcomes.append("refused")

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes.count("won") == 1
    assert outcomes.count("refused") == 7
    admission.mark_attempt(directory, "FAILED")
    assert admission.read_json(admission.attempt_path(directory))["state"] == "FAILED"
    assert admission.attempt_path(directory).is_file()


def test_evidence_dir_alone_refuses_a_new_attempt(tmp_path: Path) -> None:
    cell = _cell("pager.P2.r2")
    directory = admission.cell_dir(tmp_path, cell)
    bundle = admission.evidence_dir(directory)
    bundle.mkdir(parents=True)
    (bundle / "session.json").write_text("{}", encoding="utf-8")
    assert "evidence already exists" in (admission.prior_attempt(directory) or "")
    with pytest.raises(AttemptAlreadyStarted):
        admission.claim_attempt(
            directory, cell=cell, preflight_digest="p", authorization_digest="a", source_digest="s"
        )


def test_authorization_needs_live_intent_digests_and_a_ceiling(tmp_path: Path) -> None:
    cell = _cell()
    preflight = admission.preflight(
        evidence_root=tmp_path / "evidence", require_offline_report=False
    )
    record = {
        "status": "APPROVED",
        "approved_by": "owner",
        "approval_commit": "abc123",
        "approved_at": "2026-11-20T00:00:00Z",
        "cells": [cell.cell_id],
        "approved_digests": {
            "protocol": preflight["protocol_digest"],
            "plan": preflight["plan_digest"],
            "runtime": preflight["runtime_digest"],
            "runner_source": preflight["runner_source_digest"],
        },
        "cost_ceiling_usd": 5,
    }
    assert admission.authorization_gate(record, cell=cell, preflight_result=preflight, live=True)[
        "ready"
    ]
    offline_gate = admission.authorization_gate(
        None, cell=cell, preflight_result=preflight, live=False
    )
    assert not offline_gate["ready"]  # no record is still no record, even outside live mode
    bad = dict(record, approved_digests={})
    problems = admission.authorization_gate(bad, cell=cell, preflight_result=preflight, live=True)[
        "problems"
    ]
    assert any("digest_mismatch" in item for item in problems)


async def test_live_execution_without_authorization_never_claims(tmp_path: Path) -> None:
    request = CellRequest(
        cell=_cell(),
        runs_root=tmp_path / "runs",
        evidence_root=tmp_path / "evidence",
        live=True,
        sandbox_probe=lambda: {
            "ok": True,
            "code": "SANDBOX_HEALTH_READY",
            "endpoint": "fake:8080",
            "status_code": 200,
            "detail": "healthy",
        },
    )
    # Refused before any claim. After the Stage B-E2F harness edit, freeze 1.4 no longer describes
    # the live tree, so a live run is refused by preflight before it reaches the authorization gate;
    # both refusals happen before a marker and before a provider call.
    with pytest.raises(ExecutionBlocked, match="preflight blocked|not authorized"):
        await runner.execute_cell(request)
    assert not admission.attempt_path(
        admission.cell_dir(request.evidence_root, request.cell)
    ).is_file()


# --------------------------------------------------------------------------- collection


async def _offline_cell(
    tmp_path: Path,
    cell_id: str,
    *,
    session_kwargs: dict[str, Any] | None = None,
    error: str | None = None,
    omit: bool = False,
    host_oracle: Any = "DEFAULT",
) -> Any:
    document = _document()
    cell = provision.cell_from_plan(document, cell_id)
    kwargs = session_kwargs or {}
    if host_oracle == "DEFAULT":
        host_oracle = {
            "source": "fake:host:test",
            "task_id": cell.task_id,
            "ok": kwargs.get("oracle", "PASS") == "PASS",
            "executed_after_main_stops": True,
        }

    async def run(options: Any) -> dict[str, Any]:
        if error == "raise_key_error":
            raise KeyError("unexpected")
        if error:
            raise RuntimeError(error)
        payload = offline.synthetic_session(options, **(session_kwargs or {}))
        if not omit:
            offline.write_synthetic_run(options, payload)
        return payload

    request = CellRequest(
        cell=cell,
        runs_root=tmp_path / "runs",
        evidence_root=tmp_path / "evidence",
        protocol_document=document,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
        run_fn=run,
        host_oracle=host_oracle,
    )
    return await runner.execute_cell(request)


async def test_offline_direct_pass_writes_a_complete_bundle(tmp_path: Path) -> None:
    result = await _offline_cell(tmp_path, "slugkit.P1.r1")
    assert result.ok and result.failure_kind == "none", result.detail
    bundle = result.evidence_dir
    assert bundle is not None
    assert collector.completeness(bundle) == []
    analysis = json.loads((bundle / "analysis.json").read_text(encoding="utf-8"))
    assert analysis["schema_version"] == schema.SCHEMA_VERSION
    assert analysis["analyzer_version"] == reconstruct.ANALYZER_VERSION
    assert analysis["harness_source_digest"].startswith("sha256:")
    assert analysis["valid"] is True
    assert schema.validate_record(analysis["record"]) == []
    manifest = json.loads((bundle / "evidence_manifest.json").read_text(encoding="utf-8"))
    assert "session.json" in manifest["files"]
    marker = admission.attempt_path(admission.cell_dir(tmp_path / "evidence", result.cell))
    assert admission.read_json(marker)["state"] == "COLLECTED"
    assert admission.read_json(marker)["retries_allowed"] is False


async def test_failure_cases_keep_marker_and_do_not_retry(tmp_path: Path) -> None:
    cases = (
        (
            "pager.P1.r2",
            {"status": "failed", "acceptance": "FAIL", "stop": "budget_exceeded"},
            "budget_exhaustion",
        ),
        (
            "zonelint.P2.r2",
            {"status": "failed", "acceptance": "FAIL", "oracle": "FAIL"},
            "oracle_failed",
        ),
    )
    for cell_id, kwargs, expected in cases:
        result = await _offline_cell(tmp_path, cell_id, session_kwargs=kwargs)
        assert result.failure_kind == expected, (cell_id, result.detail)
        assert result.ok is False
        marker = admission.attempt_path(admission.cell_dir(tmp_path / "evidence", result.cell))
        assert admission.read_json(marker)["state"] == "FAILED"
        assert admission.read_json(marker)["retries_allowed"] is False


async def test_provider_and_collector_failures_are_named(tmp_path: Path) -> None:
    provider = await _offline_cell(tmp_path, "ledgerclose.P1.r1", error="provider transport failed")
    assert provider.failure_kind == "provider_exception"
    crashed = await _offline_cell(tmp_path, "ledgerclose.P2.r1", omit=True)
    assert crashed.failure_kind == "collector_failure"


def test_recover_evidence_rejects_incomplete_bundles(tmp_path: Path) -> None:
    cell = _cell()
    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    refused = collector.recover_evidence(
        incomplete, cell=cell, source_digest=admission.source_identity()["digest"]
    )
    assert refused["ok"] is False and refused["missing_raw"]
    assert "session.json" in refused["missing_raw"]
    assert not (incomplete / "session.json").exists()


async def test_recovery_regenerates_a_real_bundle(tmp_path: Path) -> None:
    result = await _offline_cell(tmp_path, "retrygate.P1.r1")
    assert result.evidence_dir is not None
    (result.evidence_dir / "analysis.json").unlink()
    recovered = collector.recover_evidence(
        result.evidence_dir,
        cell=result.cell,
        source_digest=admission.source_identity()["digest"],
    )
    assert recovered["ok"] is True, recovered.get("missing")
    assert (result.evidence_dir / "analysis.json").is_file()


# --------------------------------------------------------------------------- CLI, freeze


def test_runner_cli_has_no_run_flag_and_refuses_live_without_authorization(tmp_path: Path) -> None:
    help_text = (
        __import__("subprocess")
        .run(
            [sys.executable, "-m", "experiments.stage_benchmark.execution.runner", "--help"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        .stdout
    )
    assert __import__("re").search(r"(?<![\w-])--run(?![\w-])", help_text) is None
    assert "--execute-cell" in help_text
    legacy = __import__("subprocess").run(
        [sys.executable, str(REPO_ROOT / "experiments/stage_benchmark/run_stage_b.py"), "--run"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert legacy.returncode != 0

    result = runner.main(["--execute-cell", "slugkit.P1.r1", "--evidence-root", str(tmp_path)])
    assert result == 3


def test_preflight_cli_writes_a_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI writes the tracked preflight artifact; keep this test's report in the temp tree.
    monkeypatch.setattr(admission, "PREFLIGHT_PATH", tmp_path / "preflight.json")
    monkeypatch.setattr(
        network,
        "probe_runtime_health",
        lambda: {
            "ok": True,
            "code": "SANDBOX_HEALTH_READY",
            "endpoint": "fake:8080",
            "status_code": 200,
            "detail": "healthy",
            "bypass": "fake",
            "host": "fake",
        },
    )
    result = runner.main(["--preflight", "--evidence-root", str(tmp_path / "evidence")])
    assert result == 0
    payload = admission.read_json(admission.PREFLIGHT_PATH)
    assert payload["status"] == "READY"
    assert payload["sandbox"]["probed"] is True
    assert payload["sandbox"]["code"] == "SANDBOX_HEALTH_READY"


def test_offline_suite_covers_every_named_case() -> None:
    report = offline.run_all(Path(__import__("tempfile").mkdtemp(prefix="stage-b-e2a-test-")))
    assert report["passed"] is True
    assert set(offline.CASES).issubset(report["case_names"])
    assert report["network_calls"] == 0
    assert len(report["cases"]) == len(offline.CASES)


def test_execution_freeze_is_independent_of_the_e1_freeze() -> None:
    e1 = admission.read_json(admission.EXECUTION_DIR.parent / "freeze.json")
    frozen = admission.read_json(admission.FREEZE_PATH)
    assert frozen["freeze_version"] in {
        "stage-b-execution/1.0",
        "stage-b-execution/1.1",
        "stage-b-execution/1.2",
        "stage-b-execution/1.3",
        "stage-b-execution/1.4",
    }
    archived_digests = {
        path.name: admission.digest_bytes(path.read_bytes())
        for path in admission.ARCHIVE_DIR.glob("freeze-*.json")
    }
    assert frozen["supersedes"]["digest"] in archived_digests.values()
    assert frozen["protocol"]["digest"] == e1["protocol"]["digest"]
    assert frozen["plan_digest"] == e1["plan"]["digest"]
    assert frozen["calibration"]["in_matrix"] is False
    assert frozen["offline"]["passed"] is True
    # Freeze 1.4 is the historical record of the harness that ran before Stage B-E2F.
    # ``source_identity`` digests smoke.py, which the provisioning gate changed, so the recorded
    # digest no longer equals the live tree and must not be rewritten to make it equal. What the
    # record still has to satisfy is its own tamper-evidence; the live mismatch is what makes
    # ``runner_source_digest_matches_freeze`` block a live cell run until a new freeze is written.
    recorded = frozen["source"]
    payload = json.dumps(recorded["files"], sort_keys=True, separators=(",", ":"))
    assert recorded["digest"] == admission.digest_text(payload)
    assert set(recorded["files"]) == set(admission.SOURCE_FILES)
    assert recorded["file_count"] == len(recorded["files"])


def test_live_preflight_binds_the_harness_source_to_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drifting offline is allowed; drifting into a live run is not.

    ``require_offline_report`` is what separates the two modes: an unfinished harness edit must be
    able to produce the report a new freeze is written from, and it must not be able to spend
    money. The rule is exercised here in both directions, so neither "always pass" nor
    "always block" would satisfy it.
    """

    frozen = admission.read_json(admission.FREEZE_PATH)
    current = admission.source_identity()["digest"]
    label = "runner_source_digest_matches_freeze"

    def _check(result: dict[str, Any]) -> dict[str, Any]:
        return {item["label"]: item for item in result["checks"]}[label]

    def _freeze_with(digest: str, name: str) -> Path:
        payload = json.loads(json.dumps(frozen))
        payload["source"]["digest"] = digest
        path = tmp_path / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return path

    monkeypatch.setattr(admission, "FREEZE_PATH", _freeze_with("sha256:" + "0" * 64, "stale.json"))
    live = admission.preflight(evidence_root=tmp_path / "live", live=True)
    offline = admission.preflight(evidence_root=tmp_path / "offline", require_offline_report=False)

    monkeypatch.setattr(admission, "FREEZE_PATH", _freeze_with(current, "current.json"))
    matched = admission.preflight(evidence_root=tmp_path / "matched", live=True)

    assert _check(live)["ok"] is False
    assert live["status"] == "BLOCKED"
    assert label in " ".join(live["problems"])
    assert _check(offline)["ok"] is True
    assert _check(matched)["ok"] is True


# --------------------------------------------------------------------------- real composition


async def test_remaining_terminal_kinds_are_named_not_swallowed(tmp_path: Path) -> None:
    cases = (
        (
            "pager.P3.r1",
            {"status": "failed", "acceptance": "FAIL", "stop": "max_turns"},
            "main_max_turns",
        ),
        (
            "zonelint.P3.r1",
            {"public": "FAIL", "status": "failed", "acceptance": "FAIL"},
            "public_tests_failed",
        ),
        ("ledgerclose.P1.r2", {"error": "sandbox close failed"}, "sandbox_cleanup"),
        ("ledgerclose.P3.r2", {"error": "raise_key_error"}, "aggregation_failure"),
    )
    for cell_id, kwargs, expected in cases:
        error = kwargs.pop("error", None)
        result = await _offline_cell(tmp_path, cell_id, session_kwargs=kwargs, error=error)
        assert result.failure_kind == expected, (cell_id, result.failure_kind, result.detail)


def test_evidence_is_scrubbed_before_publication(tmp_path: Path) -> None:
    cell = _cell()
    bundle = admission.evidence_dir(admission.cell_dir(tmp_path / "evidence", cell))
    bundle.mkdir(parents=True)
    (bundle / "session.json").write_text(
        json.dumps({"leak": BY_ID["slugkit"].oracle_source}), encoding="utf-8"
    )
    redactions = collector.scrub_evidence(bundle)
    assert "session.json" in redactions
    assert BY_ID["slugkit"].oracle_source not in (bundle / "session.json").read_text(
        encoding="utf-8"
    )


def test_freeze_never_clears_prior_cell_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission, "FREEZE_PATH", tmp_path / "freeze.json")
    # Keep the archive copies in the temp tree: a test must not add files to the repository.
    monkeypatch.setattr(admission, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(admission, "ARCHIVE_FREEZE_PATH", tmp_path / "archive/freeze-1.0.json")
    monkeypatch.setattr(admission, "ARCHIVE_11_FREEZE_PATH", tmp_path / "archive/freeze-1.1.json")
    cell = _cell("cascade.P2.r1")
    directory = admission.cell_dir(tmp_path / "evidence", cell)
    admission.claim_attempt(
        directory, cell=cell, preflight_digest="p", authorization_digest="a", source_digest="s"
    )
    marker = admission.attempt_path(directory)
    before = marker.read_bytes()
    admission.write_freeze(
        offline_report=admission.read_json(admission.OFFLINE_PATH),
        preflight_result=admission.preflight(
            evidence_root=tmp_path / "evidence", require_offline_report=False
        ),
    )
    assert marker.read_bytes() == before
    assert admission.prior_attempt(directory) is not None


def test_recovery_cli_is_read_only(tmp_path: Path) -> None:
    result = asyncio.run(_offline_cell(tmp_path, "cascade.P1.r1"))
    assert result.evidence_dir is not None
    code = runner.main(
        [
            "--recover-evidence",
            "cascade.P1.r1",
            "--evidence-root",
            str(tmp_path / "evidence"),
        ]
    )
    assert code == 0
    recovery = admission.read_json(
        admission.cell_dir(tmp_path / "evidence", result.cell) / "recovery.json"
    )
    assert recovery["status"] == "RECOVERED"
    assert recovery["ok"] is True


async def test_options_compose_a_real_run_goal_with_scripted_provider(tmp_path: Path) -> None:
    """The one true composition check: run_goal itself, with a fake LLM and a fake sandbox."""

    document = _document()
    cell = provision.cell_from_plan(document, "slugkit.P1.r1")
    fixture = provision.materialize_fixture(tmp_path / "fixture", BY_ID["slugkit"])
    rig = PipelineRig()
    rig.main_script = [
        tool_call("file_read", {"path": "slugkit/core.py"}, "read"),
        tool_call(
            "file_edit",
            {
                "path": "slugkit/core.py",
                "old_string": '    raise NotImplementedError("slugify is not implemented yet")',
                "new_string": '    return "-".join(words(text))',
            },
            "edit",
        ),
        text("MAIN-FINAL: slug rules implemented; harness verifies."),
    ]
    provider = FakeSandboxProvider(tmp_path / "sandboxes")
    options = provision.build_options(
        cell=cell,
        fixture_repo=fixture,
        runs_root=tmp_path / "runs",
        run_id="stage-b-e2a-composition",
        document=document,
        provider_factory=rig.build,
        sandbox_factory=lambda _root: provider,
        offline=True,
    )
    assert options.acceptance is not None
    assert options.run_id is not None
    options = replace(
        options, acceptance=replace(options.acceptance, dynamic_command=None, require_dynamic=False)
    )
    session = await run_goal(options)
    assert options.run_id is not None

    assert session["status"] == "succeeded", session.get("failure")
    assert session["isolation"] == "sandbox"
    assert session["main_prompt"]["digest"] == protocol.prompt_spec("P1").digest
    assert provider.destroy_count == 1
    assert (tmp_path / "runs" / options.run_id / "session.json").is_file()
    assert (tmp_path / "runs" / options.run_id / "tool_conversations.json").is_file()
