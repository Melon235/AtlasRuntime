"""Preflight, authorization and one-shot attempt protection for Stage B cells.

The live runner may only start after (1) every frozen identity has been re-derived from bytes,
(2) the offline fake-provider suite has passed, and (3) an explicit authorization record has been
supplied. A successful preflight is not itself permission to call a model.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import protocol
from .. import run_stage_b as stage_runner
from ..tasks import BY_ID
from ..tasks.base import (
    fixture_digest,
    goal_digest,
    oracle_digest,
    reference_digest,
    wrong_variants_digest,
)
from .models import Attempt, AttemptAlreadyStarted, CellIdentity

EXECUTION_DIR = Path(__file__).resolve().parent
#: The original E2A freeze is archived byte-for-byte; it is never overwritten.
E2A_FREEZE_PATH = EXECUTION_DIR / "freeze.json"
ARCHIVE_DIR = EXECUTION_DIR / "archive"
ARCHIVE_FREEZE_PATH = ARCHIVE_DIR / "freeze-1.0.json"
FREEZE_11_PATH = EXECUTION_DIR / "freeze-1.1.json"
ARCHIVE_11_FREEZE_PATH = ARCHIVE_DIR / "freeze-1.1.json"
FREEZE_PATH = EXECUTION_DIR / "freeze-1.2.json"
OFFLINE_PATH = EXECUTION_DIR / "offline.json"
PREFLIGHT_PATH = EXECUTION_DIR / "preflight.json"

#: Only these source files are digested. Generated reports are not part of the harness identity.
SOURCE_FILES = (
    "__init__.py",
    "models.py",
    "admission.py",
    "provision.py",
    "collector.py",
    "runner.py",
    "offline.py",
    "adapter.py",
    "network.py",
)


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def source_identity() -> dict[str, Any]:
    """Digest the execution harness itself, independently of the frozen analysis harness."""

    files = {name: digest_bytes((EXECUTION_DIR / name).read_bytes()) for name in SOURCE_FILES}
    payload = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {
        "root": str(EXECUTION_DIR),
        "file_count": len(files),
        "files": files,
        "digest": digest_text(payload),
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_digest(payload: Mapping[str, Any]) -> str:
    return digest_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def cell_dir(evidence_root: Path, cell: CellIdentity) -> Path:
    return evidence_root / cell.cell_id / f"r{cell.repetition}"


def attempt_path(cell_dir_path: Path) -> Path:
    return cell_dir_path / "attempts" / "attempt-1.json"


def evidence_dir(cell_dir_path: Path) -> Path:
    return cell_dir_path / "evidence"


def prior_attempt(cell_dir_path: Path) -> str | None:
    """Any marker or evidence already on disk is a reason to refuse, not a reason to clean."""

    marker = attempt_path(cell_dir_path)
    if marker.is_file():
        return f"attempt marker exists: {marker}"
    bundle = evidence_dir(cell_dir_path)
    if bundle.is_dir() and any(bundle.iterdir()):
        return f"evidence already exists: {bundle}"
    return None


def claim_attempt(
    cell_dir_path: Path,
    *,
    cell: CellIdentity,
    preflight_digest: str,
    authorization_digest: str,
    source_digest: str,
) -> Attempt:
    """Atomically claim one cell. The marker is never removed, including after a failure."""

    existing = prior_attempt(cell_dir_path)
    if existing is not None:
        raise AttemptAlreadyStarted(existing)
    marker = attempt_path(cell_dir_path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    attempt = Attempt(
        cell=cell,
        state="CLAIMED",
        claimed_at=datetime.now(UTC).isoformat(),
        preflight_digest=preflight_digest,
        authorization_digest=authorization_digest,
        runner_source_digest=source_digest,
        path=marker,
    )
    body = json.dumps(attempt.to_dict(), indent=2, sort_keys=True).encode("utf-8")
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise AttemptAlreadyStarted(f"attempt marker already exists: {marker}") from error
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return attempt


def mark_attempt(cell_dir_path: Path, state: str) -> None:
    """Advance the marker's state in place. This never removes or reissues the claim."""

    marker = attempt_path(cell_dir_path)
    if not marker.is_file():
        raise RuntimeError(f"no attempt marker to update: {marker}")
    payload = read_json(marker)
    payload["state"] = state
    marker.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _check(ok: bool, label: str, detail: str = "") -> dict[str, Any]:
    return {"ok": bool(ok), "label": label, "detail": detail}


def preflight(
    *,
    evidence_root: Path,
    cell: CellIdentity | None = None,
    live: bool = False,
    protocol_document: Mapping[str, Any] | None = None,
    offline_report: Mapping[str, Any] | None = None,
    require_offline_report: bool = True,
    probe_sandbox: bool = False,
    sandbox_probe: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Re-derive every frozen identity. Read-only: this function writes nothing."""

    document = dict(protocol_document or protocol.read_protocol())
    checks: list[dict[str, Any]] = []
    frozen = read_json(stage_runner.FREEZE_PATH)

    analysis = stage_runner.check_all()
    checks.append(_check(analysis["status"] == "READY", "protocol_v1_1_ready", analysis["status"]))
    checks.append(
        _check(
            document.get("protocol_version") == protocol.PROTOCOL_VERSION == "1.1.0",
            "protocol_version_is_1_1",
            str(document.get("protocol_version")),
        )
    )
    checks.append(
        _check(
            document.get("analysis_schema_version") == protocol.ANALYSIS_SCHEMA_VERSION,
            "analysis_schema_matches",
            str(document.get("analysis_schema_version")),
        )
    )

    runtime = stage_runner.source_identity()
    checks.append(
        _check(
            runtime["tree_digest"] == frozen["runtime"]["tree_digest"],
            "runtime_source_unchanged",
            runtime["tree_digest"],
        )
    )
    checks.append(
        _check(
            digest_bytes(protocol.PROTOCOL_PATH.read_bytes()) == frozen["protocol"]["digest"],
            "protocol_digest_unchanged",
            frozen["protocol"]["digest"],
        )
    )
    plan = read_json(protocol.PLAN_PATH)
    checks.append(
        _check(
            plan["plan"]["digest"]
            == frozen["plan"]["digest"]
            == stage_runner.HISTORICAL_PLAN_DIGEST,
            "plan_digest_unchanged",
            plan["plan"]["digest"],
        )
    )

    prompt_ok = True
    for entry in document["arms"]:
        arm = entry["arm_id"]
        spec = protocol.prompt_spec(arm)
        prompt_ok = prompt_ok and (
            entry["declared_digest"] == spec.digest == frozen["prompts"][arm]["digest"]
            and spec.version == entry["label"]
        )
    checks.append(_check(prompt_ok, "prompt_versions_and_digests_unchanged", "P1/P2/P3"))

    task_ok = True
    for task_id, entry in frozen["tasks"].items():
        spec = BY_ID[task_id]
        task_ok = task_ok and (
            fixture_digest(spec) == entry["fixture_digest"]
            and goal_digest(spec) == entry["goal_digest"]
            and oracle_digest(spec) == entry["oracle_digest"]
            and reference_digest(spec) == entry["reference_digest"]
            and wrong_variants_digest(spec) == entry["wrong_variants_digest"]
        )
    checks.append(_check(task_ok, "task_and_oracle_digests_unchanged", "6 tasks"))

    checks.append(
        _check(
            document["run_config"] == frozen["config"], "run_config_unchanged", "deepseek / 500000"
        )
    )
    checks.append(
        _check(
            frozen["analysis"]["schema_version"] == protocol.ANALYSIS_SCHEMA_VERSION
            and frozen["analysis"]["reconstruct_version"] == "stage-b-reconstruct/1.1",
            "analysis_versions_unchanged",
            str(frozen["analysis"]),
        )
    )

    source = source_identity()
    freeze_source = None
    if FREEZE_PATH.is_file():
        freeze_source = read_json(FREEZE_PATH).get("source", {}).get("digest")
    # The source-freeze binding is a live-authorization rule. An offline suite must be able to
    # re-run after a harness edit in order to produce the report a new freeze is written from.
    checks.append(
        _check(
            (not require_offline_report)
            or freeze_source is None
            or freeze_source == source["digest"],
            "runner_source_digest_matches_freeze",
            f"current={source['digest']} frozen={freeze_source}"
            + ("" if require_offline_report else " (not binding offline)"),
        )
    )

    report = (
        offline_report
        if offline_report is not None
        else (read_json(OFFLINE_PATH) if OFFLINE_PATH.is_file() else {})
    )
    checks.append(
        _check(
            (not require_offline_report) or bool(report.get("passed")),
            "fake_provider_suite_passed",
            str(report.get("passed")),
        )
    )

    from . import network  # local import: keeps the module graph acyclic
    from .provision import isolation_checks  # local import: provision imports admission constants

    isolation = isolation_checks(document)
    checks.append(_check(isolation["ok"], "host_oracle_isolated", "; ".join(isolation["problems"])))

    sandbox: dict[str, Any] = {
        "probed": bool(probe_sandbox),
        "endpoint": network.sandbox_endpoint()[0],
    }
    if probe_sandbox:
        probe = (sandbox_probe or network.probe_runtime_health)()
        sandbox.update(probe)
        checks.append(
            _check(
                bool(probe.get("ok")),
                "sandbox_health",
                f"{probe.get('code')} endpoint={probe.get('endpoint')} "
                f"status={probe.get('status_code')} detail={probe.get('detail')}",
            )
        )

    for label, live_path, archived_path in (
        ("e2a_freeze_preserved", E2A_FREEZE_PATH, ARCHIVE_FREEZE_PATH),
        ("e2a_freeze_1_1_preserved", FREEZE_11_PATH, ARCHIVE_11_FREEZE_PATH),
    ):
        if live_path.is_file() and archived_path.is_file():
            archived_ok = digest_bytes(archived_path.read_bytes()) == digest_bytes(
                live_path.read_bytes()
            )
        else:
            archived_ok = not archived_path.is_file()
        checks.append(_check(archived_ok, label, str(archived_path)))

    if cell is not None:
        cells = {item["cell_id"] for item in plan["plan"]["cells"]}
        identity_ok = (
            (cell.cell_id in cells) if cell.in_matrix else cell.cell_id not in cells
        ) and cell.arm_id in protocol.ARM_IDS
        if cell.in_matrix:
            identity_ok = identity_ok and BY_ID[cell.task_id].task_id == cell.task_id
        else:
            from .provision import verify_calibration_identity

            calibration_problems = verify_calibration_identity()
            identity_ok = identity_ok and not calibration_problems
        checks.append(_check(identity_ok, "cell_identity_matches_plan", cell.cell_id))
        conflict = prior_attempt(cell_dir(evidence_root, cell))
        checks.append(_check(conflict is None, "no_cell_marker_or_evidence", conflict or "clear"))
    else:
        conflicts = (
            [
                str(path)
                for path in evidence_root.glob("*/r*/attempts/attempt-1.json")
                if path.is_file()
            ]
            if evidence_root.is_dir()
            else []
        )
        checks.append(
            _check(not conflicts, "no_existing_cell_markers", "; ".join(conflicts) or "clear")
        )

    problems = [f"{item['label']}: {item['detail']}" for item in checks if not item["ok"]]
    result = {
        "status": "READY" if not problems else "BLOCKED",
        "live": bool(live),
        "problems": problems,
        "checks": checks,
        "protocol_version": document["protocol_version"],
        "protocol_digest": digest_bytes(protocol.PROTOCOL_PATH.read_bytes()),
        "plan_digest": plan["plan"]["digest"],
        "runtime_digest": runtime["tree_digest"],
        "runner_source_digest": source["digest"],
        "offline_report_digest": canonical_digest(report) if report else None,
        "sandbox": sandbox,
    }
    result["digest"] = canonical_digest(
        {key: value for key, value in result.items() if key != "digest"}
    )
    return result


def authorization_gate(
    record: Mapping[str, Any] | None,
    *,
    cell: CellIdentity,
    preflight_result: Mapping[str, Any],
    live: bool,
) -> dict[str, Any]:
    """An explicit, cell-scoped, digest-bound approval is required for a live provider call."""

    problems: list[str] = []
    if not live:
        problems.append("live_intent_not_confirmed")
    if not record:
        problems.append("no_authorization_record")
        record = {}
    if record.get("status") != "APPROVED":
        problems.append(f"status={record.get('status')!r}")
    for field in ("approved_by", "approval_commit", "approved_at"):
        if not record.get(field):
            problems.append(f"missing_{field}")
    cells = set(record.get("cells") or ())
    if cell.cell_id not in cells:
        problems.append(f"cell_not_authorized: {cell.cell_id}")
    expected = {
        "protocol": preflight_result.get("protocol_digest"),
        "plan": preflight_result.get("plan_digest"),
        "runtime": preflight_result.get("runtime_digest"),
        "runner_source": preflight_result.get("runner_source_digest"),
    }
    approved = record.get("approved_digests") or {}
    if not isinstance(approved, Mapping):
        approved = {}
    for name, value in expected.items():
        if approved.get(name) != value:
            problems.append(f"digest_mismatch:{name}")
    if record.get("consumed") is True:
        problems.append("authorization_already_consumed")
    ceiling = record.get("cost_ceiling_usd")
    if not isinstance(ceiling, (int, float)) or ceiling <= 0:
        problems.append("no_cost_ceiling")
    return {
        "ready": not problems,
        "problems": problems,
        "digest": canonical_digest(record),
    }


def write_freeze(
    *, offline_report: Mapping[str, Any], preflight_result: Mapping[str, Any]
) -> dict[str, Any]:
    """Write a versioned execution freeze. Neither E1 nor the E2A original is overwritten."""

    if not E2A_FREEZE_PATH.is_file():
        raise RuntimeError(f"original E2A freeze is missing: {E2A_FREEZE_PATH}")
    original_digest = digest_bytes(E2A_FREEZE_PATH.read_bytes())
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    if not ARCHIVE_FREEZE_PATH.is_file():
        shutil.copy2(E2A_FREEZE_PATH, ARCHIVE_FREEZE_PATH)
    if digest_bytes(ARCHIVE_FREEZE_PATH.read_bytes()) != original_digest:
        raise RuntimeError("the archived E2A freeze does not match the original")

    if not FREEZE_11_PATH.is_file():
        raise RuntimeError(f"the 1.1 execution freeze is missing: {FREEZE_11_PATH}")
    previous_digest = digest_bytes(FREEZE_11_PATH.read_bytes())
    if not ARCHIVE_11_FREEZE_PATH.is_file():
        shutil.copy2(FREEZE_11_PATH, ARCHIVE_11_FREEZE_PATH)
    if digest_bytes(ARCHIVE_11_FREEZE_PATH.read_bytes()) != previous_digest:
        raise RuntimeError("the archived 1.1 execution freeze does not match the live copy")

    document = {
        "stage": "B-E2C-CALIBRATION-RETRY",
        "freeze_version": "stage-b-execution/1.2",
        "supersedes": {
            "version": "stage-b-execution/1.1",
            "live_path": str(FREEZE_11_PATH),
            "archive_path": str(ARCHIVE_11_FREEZE_PATH),
            "digest": previous_digest,
        },
        "history": [
            {
                "version": "stage-b-execution/1.0",
                "archive_path": str(ARCHIVE_FREEZE_PATH),
                "digest": original_digest,
            },
            {
                "version": "stage-b-execution/1.1",
                "archive_path": str(ARCHIVE_11_FREEZE_PATH),
                "digest": previous_digest,
            },
        ],
        "frozen_at": datetime.now(UTC).isoformat(),
        "git": stage_runner.git_identity(),
        "source": source_identity(),
        "protocol": {
            "version": protocol.PROTOCOL_VERSION,
            "digest": digest_bytes(protocol.PROTOCOL_PATH.read_bytes()),
            "analysis_schema_version": protocol.ANALYSIS_SCHEMA_VERSION,
        },
        "e1_freeze": digest_bytes(stage_runner.FREEZE_PATH.read_bytes()),
        "plan_digest": read_json(protocol.PLAN_PATH)["plan"]["digest"],
        "offline": {
            "digest": canonical_digest(offline_report),
            "passed": bool(offline_report.get("passed")),
            "cases": len(offline_report.get("cases") or []),
        },
        "preflight_digest": preflight_result.get("digest"),
        "analysis": {
            "execution_analysis_version": "stage-b-execution-analysis/1.1",
            "e1_analyser_unchanged": "stage-b-reconstruct/1.1",
        },
        "calibration": {
            "task": "workgrid",
            "cell_id": "CALIBRATION.workgrid.c2",
            "previous_attempt": "CALIBRATION.workgrid.c1",
            "in_matrix": False,
            "status": "PREPARED_NOT_RUN",
            "reason_not_run": "this stage may not call the provider",
            "identity": "experiments/stage_s2_prompt_v21/freeze.json",
            "attempts_authorised": 1,
        },
    }
    FREEZE_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def write_preflight(result: Mapping[str, Any]) -> Path:
    PREFLIGHT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return PREFLIGHT_PATH
