"""Stage B-E2F closure: the versioned execution freeze chain, now pointing at 1.6.

The defect these tests exist for is narrow and was real: ``write_freeze`` hard-coded
``stage-b-execution/1.4`` and wrote it to ``freeze-1.4.json``, so ``runner --freeze`` would have
overwritten a freeze that a completed live run was authorized against. Every test here is offline:
no provider, no sandbox, no calibration, no benchmark cell. Nothing writes inside the repository:
the archive and the active freeze are redirected into ``tmp_path`` before a writer runs.

S05 moved the pointer to ``1.6`` to prepare the writer for the analysis-1.2 harness, and appended
``1.5`` to the immutable chain. Writing 1.6 is a separate, reviewed step: without the file no live
cell can start, which the last tests in this module assert.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

from experiments.stage_benchmark import protocol  # noqa: E402
from experiments.stage_benchmark.execution import admission, provision, runner  # noqa: E402

EVIDENCE_DIR = REPO_ROOT / "experiments/stage_b_smoke/e2f-provisioning"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _c3_index(tmp_path: Path, **overrides: Any) -> Path:
    """A synthetic sealed c3 index. Its file paths point nowhere real, so nothing is verified."""

    payload: dict[str, Any] = {
        "stage": "B-C3-CALIBRATION",
        "cell": "CALIBRATION.workgrid.c3",
        "repetition": 0,
        "git_commit": "7a84cb7b39a18162082b9bd69284b87eef245532",
        "execution_freeze": "stage-b-execution/1.5",
        "runner_source_digest": "sha256:" + "8" * 64,
        "claim": {
            "sha256": "sha256:" + "a" * 64,
            "state": "COLLECTED",
            "claimed_at": "2026-11-24T03:12:56+00:00",
            "retries_allowed": False,
            "runner_source_digest": "sha256:" + "8" * 64,
            "preflight_digest": "sha256:" + "9" * 64,
        },
        "verdicts": {
            "cell_status": "COMPLETED",
            "execution_integrity": "OK",
            "goal_correctness": "PASS",
            "oracle_agreement": "AGREE",
            "host_oracle_source": "host:experiments/stage_s2_prompt_v21/s2_contract",
        },
        "usage": {"requests": 12, "system_total_tokens": 167986},
        "files": [
            {
                "path": "calibration/SYNTHETIC/evidence/public_tests.json",
                "bytes": 10,
                "sha256": "sha256:" + "b" * 64,
            },
            {
                "path": "calibration/SYNTHETIC/evidence/oracle.json",
                "bytes": 10,
                "sha256": "sha256:" + "c" * 64,
            },
        ],
    }
    payload.update(overrides)
    path = tmp_path / "c3-index" / "c3-evidence-index.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the active freeze, the archive and the c3 provenance at the temp tree."""

    monkeypatch.setattr(admission, "FREEZE_PATH", tmp_path / "freeze-1.6.json")
    monkeypatch.setattr(admission, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setattr(admission, "CALIBRATION_INDEX_PATH", _c3_index(tmp_path))
    return tmp_path / "freeze-1.6.json"


def _write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    target = _isolate(tmp_path, monkeypatch)
    report = admission.read_json(admission.OFFLINE_PATH)
    preflight = admission.preflight(
        evidence_root=tmp_path / "evidence", offline_report=report, require_offline_report=False
    )
    assert preflight["status"] == "READY", preflight["problems"]
    return target, admission.write_freeze(offline_report=report, preflight_result=preflight)


def _labels(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["label"]: item for item in result["checks"]}


# --------------------------------------------------------------------------- the version pointer


def test_the_active_pointer_is_1_6_and_1_5_is_the_last_spent_version() -> None:
    assert admission.FREEZE_VERSION == "stage-b-execution/1.6"
    assert admission.FREEZE_PATH.name == "freeze-1.6.json"
    assert [version for version, _path, _digest in admission.FROZEN_CHAIN] == [
        "stage-b-execution/1.0",
        "stage-b-execution/1.1",
        "stage-b-execution/1.2",
        "stage-b-execution/1.3",
        "stage-b-execution/1.4",
        "stage-b-execution/1.5",
    ]
    assert admission.FROZEN_CHAIN[-1][1].name == "freeze-1.5.json"
    # The one bug this stage fixes: writing must not aim at a spent version.
    assert admission.FROZEN_CHAIN[-1][1] != admission.FREEZE_PATH


def test_the_next_freeze_is_prepared_but_not_written() -> None:
    """Moving the pointer is preparation. Without the file, no live cell may start."""

    assert not admission.FREEZE_PATH.is_file()
    live = admission.preflight(evidence_root=Path("unused"), cell=None, live=True)
    assert live["status"] == "BLOCKED"
    assert _labels(live)["active_execution_freeze_present"]["ok"] is False
    assert _labels(live)["runner_source_digest_matches_freeze"]["ok"] is False
    assert admission.frozen_chain_problems() == []


def test_the_written_freeze_declares_1_6_and_supersedes_1_5_by_its_real_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, document = _write(monkeypatch, tmp_path)
    assert target.is_file()
    assert document["freeze_version"] == "stage-b-execution/1.6"
    assert document["supersedes"]["version"] == "stage-b-execution/1.5"
    # The real SHA-256 of the bytes this worktree has, plus the rendering-free pin.
    assert document["supersedes"]["digest"] == _digest(admission.FROZEN_CHAIN[-1][1])
    assert document["supersedes"]["content_digest"] == admission.FROZEN_CHAIN[-1][2]
    assert document["supersedes"]["live_path"] == str(admission.FROZEN_CHAIN[-1][1])
    history = {item["version"]: item["content_digest"] for item in document["history"]}
    assert history == {version: digest for version, _path, digest in admission.FROZEN_CHAIN}
    assert admission.freeze_history_problems(document) == []
    # The source identity is re-derived from the worktree, never copied from a report.
    assert document["source"]["digest"] == admission.source_identity()["digest"]


# --------------------------------------------------------------------------- 1.5 is never rewritten


def test_writing_1_6_never_touches_the_bytes_of_1_5(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_14 = admission.FROZEN_CHAIN[-1][1]
    before = frozen_14.read_bytes()
    before_archive = {
        version: (admission.archive_path(version).read_bytes())
        for version, _path, _digest in admission.FROZEN_CHAIN
        if admission.archive_path(version).is_file()
    }

    _write(monkeypatch, tmp_path)

    assert frozen_14.read_bytes() == before
    assert admission.content_digest(frozen_14) == admission.FROZEN_CHAIN[-1][2]
    for version, payload in before_archive.items():
        assert admission.archive_path(version).read_bytes() == payload


def test_a_moved_1_5_blocks_the_writer_and_the_live_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tampered = tmp_path / "freeze-1.5.json"
    tampered.write_bytes(admission.FROZEN_CHAIN[-1][1].read_bytes() + b" ")
    chain = (
        *admission.FROZEN_CHAIN[:-1],
        (admission.FROZEN_CHAIN[-1][0], tampered, admission.FROZEN_CHAIN[-1][2]),
    )
    monkeypatch.setattr(admission, "FROZEN_CHAIN", chain)
    _isolate(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="chain is broken"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence", require_offline_report=False
            ),
        )
    assert not (tmp_path / "freeze-1.6.json").exists()

    blocked = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert blocked["status"] == "BLOCKED"
    assert _labels(blocked)["execution_freeze_chain_intact"]["ok"] is False
    assert "execution_freeze_chain_intact" in " ".join(blocked["problems"])


def test_an_existing_active_freeze_is_never_rewritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _document = _write(monkeypatch, tmp_path)
    written = target.read_bytes()
    with pytest.raises(RuntimeError, match="never rewritten"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence2", require_offline_report=False
            ),
        )
    assert target.read_bytes() == written


# --------------------------------------------------------------------------- live readiness


def test_a_missing_1_5_cannot_reach_live_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    live = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert live["status"] == "BLOCKED"
    assert _labels(live)["active_execution_freeze_present"]["ok"] is False
    # No freeze means no recorded binding either: absence is never a pass.
    assert _labels(live)["runner_source_digest_matches_freeze"]["ok"] is False
    # The same tree is good enough to write the freeze from.
    offline = admission.preflight(
        evidence_root=tmp_path / "evidence",
        offline_report=admission.read_json(admission.OFFLINE_PATH),
        require_offline_report=False,
    )
    assert offline["status"] == "READY", offline["problems"]


def test_a_wrong_source_digest_cannot_reach_live_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, document = _write(monkeypatch, tmp_path)
    live = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert live["status"] == "READY", live["problems"]

    stale = json.loads(json.dumps(document))
    stale["source"]["digest"] = "sha256:" + "0" * 64
    target.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    blocked = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert blocked["status"] == "BLOCKED"
    assert _labels(blocked)["runner_source_digest_matches_freeze"]["ok"] is False


def _drop_source(document: dict[str, Any]) -> None:
    document.pop("source")


def _drop_source_digest(document: dict[str, Any]) -> None:
    document["source"].pop("digest")


def _null_source_digest(document: dict[str, Any]) -> None:
    document["source"]["digest"] = None


def _empty_source_digest(document: dict[str, Any]) -> None:
    document["source"]["digest"] = ""


def _non_string_source_digest(document: dict[str, Any]) -> None:
    document["source"]["digest"] = 0


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_drop_source, id="source-missing"),
        pytest.param(_drop_source_digest, id="digest-missing"),
        pytest.param(_null_source_digest, id="digest-null"),
        pytest.param(_empty_source_digest, id="digest-empty"),
        pytest.param(_non_string_source_digest, id="digest-not-a-string"),
    ],
)
def test_a_freeze_without_a_real_source_digest_cannot_reach_live_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate: Any
) -> None:
    """A written freeze that records no binding must not authorize a live cell."""

    target, document = _write(monkeypatch, tmp_path)
    mutate(document)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    blocked = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert blocked["status"] == "BLOCKED"
    assert _labels(blocked)["runner_source_digest_matches_freeze"]["ok"] is False
    # The file is present and well formed: the binding is the only reason to refuse.
    assert _labels(blocked)["active_execution_freeze_present"]["ok"] is True


def test_an_unbound_freeze_still_allows_the_offline_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline runs are how the report a new freeze is built from gets produced."""

    target, document = _write(monkeypatch, tmp_path)
    _null_source_digest(document)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    offline = admission.preflight(
        evidence_root=tmp_path / "evidence",
        offline_report=admission.read_json(admission.OFFLINE_PATH),
        require_offline_report=False,
    )
    assert offline["status"] == "READY", offline["problems"]


def test_a_broken_history_chain_cannot_reach_live_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _document = _write(monkeypatch, tmp_path)
    payload = admission.read_json(target)
    payload["supersedes"]["content_digest"] = "sha256:" + "1" * 64
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    blocked = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert blocked["status"] == "BLOCKED"
    assert _labels(blocked)["execution_freeze_supersedes_history"]["ok"] is False

    payload = admission.read_json(target)
    payload["supersedes"]["content_digest"] = admission.FROZEN_CHAIN[-1][2]
    payload["freeze_version"] = "stage-b-execution/1.5"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    mismatched = admission.preflight(evidence_root=tmp_path / "evidence", live=True)
    assert mismatched["status"] == "BLOCKED"
    assert _labels(mismatched)["execution_freeze_supersedes_history"]["ok"] is False


# --------------------------------------------------------------------------- the CLI target


def test_the_freeze_cli_targets_1_6_and_leaves_1_5_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(admission, "PREFLIGHT_PATH", tmp_path / "preflight.json")
    frozen_15 = admission.FROZEN_CHAIN[-1][1]
    before = frozen_15.read_bytes()

    code = runner.main(["--freeze", "--evidence-root", str(tmp_path / "evidence")])

    assert code == 0
    assert target.is_file()
    assert frozen_15.read_bytes() == before
    assert admission.read_json(target)["freeze_version"] == "stage-b-execution/1.6"


# --------------------------------------------------------------------------- c3 provenance


def test_the_new_freeze_records_the_completed_calibration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _target, document = _write(monkeypatch, tmp_path)
    calibration = document["calibration"]
    assert (
        calibration["cell_id"] == provision.calibration_cell().cell_id == "CALIBRATION.workgrid.c3"
    )
    # The attempt already ran under 1.5. A new freeze records it; it never demotes it.
    assert calibration["status"] == "COMPLETED"
    assert calibration["verdict"] == "PASS"
    assert calibration["executed_under_freeze"] == "stage-b-execution/1.5"
    assert calibration["executed_commit"] == "7a84cb7b39a18162082b9bd69284b87eef245532"
    assert calibration["claim"]["state"] == "COLLECTED"
    assert calibration["claim"]["retries_allowed"] is False
    assert calibration["retry"] == "FORBIDDEN"
    assert calibration["previous_attempts"] == [
        "CALIBRATION.workgrid.c1",
        "CALIBRATION.workgrid.c2",
    ]
    assert calibration["attempts_authorised_total"] == 3
    # The new freeze authorises nothing new.
    assert calibration["authorised_this_stage"] == 0
    assert calibration["authorises_new_calibration"] is False
    assert "PREPARED_NOT_RUN" not in json.dumps(calibration)
    # The historical rulings and evidence digests travel with the record.
    assert calibration["verdicts"]["goal_correctness"] == "PASS"
    assert calibration["verdicts"]["oracle_agreement"] == "AGREE"
    assert calibration["public_tests"]["sha256"].startswith("sha256:")
    assert calibration["host_oracle"]["sha256"].startswith("sha256:")
    assert calibration["evidence_index"]["sha256"].startswith("sha256:")
    assert calibration["evidence_index"]["files"] == 2
    assert provision.CALIBRATION["attempt"] == 3
    assert provision.CALIBRATION["status"] == "COMPLETED"
    # Writing a freeze is not an approval: the gate still refuses the cell without a record.
    gate = admission.authorization_gate(
        None, cell=provision.calibration_cell(), preflight_result={"digest": "x"}, live=True
    )
    assert gate["ready"] is False
    assert "no_authorization_record" in gate["problems"]


def test_a_missing_calibration_index_refuses_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(admission, "CALIBRATION_INDEX_PATH", tmp_path / "absent.json")
    with pytest.raises(RuntimeError, match="calibration provenance is missing"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence", require_offline_report=False
            ),
        )
    assert not target.exists()


def test_a_calibration_that_was_never_completed_refuses_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _isolate(tmp_path, monkeypatch)
    index = _c3_index(
        tmp_path,
        verdicts={
            "cell_status": "FAILED",
            "execution_integrity": "FAILED",
            "goal_correctness": "UNKNOWN",
            "oracle_agreement": "UNKNOWN",
            "host_oracle_source": "UNKNOWN",
        },
    )
    monkeypatch.setattr(admission, "CALIBRATION_INDEX_PATH", index)
    with pytest.raises(RuntimeError, match="calibration provenance is inconsistent"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence", require_offline_report=False
            ),
        )
    assert not target.exists()


def test_an_indexed_evidence_digest_that_moved_refuses_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _isolate(tmp_path, monkeypatch)
    artifact = tmp_path / "public_tests.json"
    artifact.write_text("{}", encoding="utf-8")
    index = _c3_index(
        tmp_path,
        files=[
            {
                "path": str(artifact).replace("\\", "/"),
                "bytes": len(artifact.read_bytes()),
                "sha256": "sha256:" + "0" * 64,
            }
        ],
    )
    monkeypatch.setattr(admission, "CALIBRATION_INDEX_PATH", index)
    with pytest.raises(RuntimeError, match="indexed evidence changed"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence", require_offline_report=False
            ),
        )
    assert not target.exists()


def test_a_retryable_claim_refuses_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(tmp_path, monkeypatch)
    index = _c3_index(
        tmp_path,
        claim={
            "sha256": "sha256:" + "a" * 64,
            "state": "CLAIMED",
            "claimed_at": "2026-11-24T03:12:56+00:00",
            "retries_allowed": True,
        },
    )
    monkeypatch.setattr(admission, "CALIBRATION_INDEX_PATH", index)
    with pytest.raises(RuntimeError, match="calibration provenance is inconsistent"):
        admission.write_freeze(
            offline_report=admission.read_json(admission.OFFLINE_PATH),
            preflight_result=admission.preflight(
                evidence_root=tmp_path / "evidence", require_offline_report=False
            ),
        )


def test_the_prepared_c3_request_is_not_an_approval() -> None:
    """The Owner package is a request. Only the signature fields stand between it and the gate."""

    path = REPO_ROOT / "experiments/stage_b_calibration/authorization-c3.json"
    record = admission.read_json(path)
    assert record["status"] == "PENDING_OWNER_APPROVAL"
    assert record["consumed"] is False
    assert record["cells"] == ["CALIBRATION.workgrid.c3"]
    assert record["cost_ceiling_usd"] > 0
    # runner_source is bound to the freeze the request was prepared against, never to the live tree:
    # any later harness edit changes the live digest, which is exactly why c3 could only be
    # authorised against a recorded freeze.
    version = record["freeze_binding"]["execution_freeze"]
    freeze_path = admission.EXECUTION_DIR / f"freeze-{admission.version_suffix(version)}.json"
    freeze = admission.read_json(freeze_path)
    assert record["approved_digests"]["runner_source"] == freeze["source"]["digest"]

    preflight = {
        "protocol_digest": admission.digest_bytes(protocol.PROTOCOL_PATH.read_bytes()),
        "plan_digest": admission.read_json(protocol.PLAN_PATH)["plan"]["digest"],
        "runtime_digest": admission.stage_runner.source_identity()["tree_digest"],
        "runner_source_digest": freeze["source"]["digest"],
    }
    gate = admission.authorization_gate(
        record, cell=provision.calibration_cell(), preflight_result=preflight, live=True
    )
    assert gate["ready"] is False
    assert "status='PENDING_OWNER_APPROVAL'" in gate["problems"]
    assert "missing_approved_by" in gate["problems"]
    # The digests themselves no longer block: only the Owner's signature does.
    assert not any(problem.startswith("digest_mismatch") for problem in gate["problems"])
    # c3 ran once in S04 under an out-of-repo signature; the tracked request was never retroactively
    # signed, so the repository still carries a request rather than an approval.
    assert record["approved_by"] is None
    assert record["approval_commit"] is None
    assert record["approved_at"] is None


# --------------------------------------------------------------------------- E2F evidence survives


@pytest.mark.skipif(
    not EVIDENCE_DIR.is_dir(), reason="the E2F evidence only exists on the host that ran it"
)
def test_the_e2f_evidence_is_untouched_by_a_new_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {
        str(path.relative_to(EVIDENCE_DIR)): _digest(path)
        for path in sorted(EVIDENCE_DIR.rglob("*"))
        if path.is_file()
    }
    _write(monkeypatch, tmp_path)
    after = {
        str(path.relative_to(EVIDENCE_DIR)): _digest(path)
        for path in sorted(EVIDENCE_DIR.rglob("*"))
        if path.is_file()
    }
    assert after == before


def test_a_missing_provisioning_evidence_stays_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "attempts" / "attempt-absent" / "provisioning-result.json"
    monkeypatch.setattr(admission, "PROVISIONING_EVIDENCE_PATH", missing)
    monkeypatch.setattr(admission, "PROVISIONING_REVIEW_PATH", tmp_path / "review.json")

    _target, document = _write(monkeypatch, tmp_path)

    gate = document["provisioning_gate"]
    assert gate["status"] == "NOT_RUN"
    assert gate["verdict"] is None
    assert gate["digest"] is None
    assert gate["review"] == "ABSENT"
    # An unobserved exit code is never upgraded to 0 by the record itself.
    assert gate["process_exit_code"] == "UNOBSERVED"
    assert gate["retry"] == "FORBIDDEN"


@pytest.mark.skipif(
    not admission.PROVISIONING_EVIDENCE_PATH.is_file(), reason="the E2F evidence is host-local"
)
def test_the_recorded_provisioning_gate_matches_the_evidence_bytes() -> None:
    gate = admission._provisioning_gate_summary()
    assert gate["digest"] == _digest(admission.PROVISIONING_EVIDENCE_PATH)
    assert gate["status"] == "PASSED"
    assert gate["verdict"] == "PROVISIONING_SMOKE_PASS"
    assert gate["checks_total"] == gate["checks_true"]
    assert gate["unconfirmed"] == []
    assert gate["model_requests"] == 0
    assert gate["calibration_attempts"] == 0
    assert gate["benchmark_cells"] == 0
    assert gate["sandbox_destroyed"] is True
    assert gate["residual"] is False
    # The exit-code exception is recorded from the review file, never asserted by the harness.
    assert gate["review"] == "ACCEPTED_WITH_OBSERVATION_EXCEPTION"
    assert gate["process_exit_code"] == "UNOBSERVED"
    assert gate["review_digest"] == _digest(admission.PROVISIONING_REVIEW_PATH)
    assert gate["retry"] == "FORBIDDEN"
