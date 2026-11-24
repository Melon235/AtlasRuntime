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
ARCHIVE_DIR = EXECUTION_DIR / "archive"

#: Every execution freeze already spent, oldest first: version, live file, content digest.
#: The digest is the identity of the file, so a later stage may append a version but must never
#: rewrite one of these: :func:`preflight` and :func:`write_freeze` both refuse on a mismatch.
#: It is computed over LF-normalised bytes: this repository checks text out with ``eol=lf`` but
#: older worktrees render some directories as CRLF, and a rendering difference is not a content
#: difference. Any edit, including a trailing byte, still changes the digest.
FROZEN_CHAIN: tuple[tuple[str, Path, str], ...] = (
    (
        "stage-b-execution/1.0",
        EXECUTION_DIR / "freeze.json",
        "sha256:48acad3ee7adbcff2e635b9ee6101c52dad9d6cd636146bf85dfb39e96e0bd23",
    ),
    (
        "stage-b-execution/1.1",
        EXECUTION_DIR / "freeze-1.1.json",
        "sha256:437df8c480bdcb1631163741b32368584cffbf9ccfe90ea919165cd211170fb9",
    ),
    (
        "stage-b-execution/1.2",
        EXECUTION_DIR / "freeze-1.2.json",
        "sha256:5a0718b3c2354aa8fb44491b0943962725a4e5cbd202a9fa6def505d8b272751",
    ),
    (
        "stage-b-execution/1.3",
        EXECUTION_DIR / "freeze-1.3.json",
        "sha256:d177f8f99c311e790a8571b25129194e77d7c6f9e9071deb01edb4f740c7702e",
    ),
    (
        "stage-b-execution/1.4",
        EXECUTION_DIR / "freeze-1.4.json",
        "sha256:eaee6d8662c89fcd7c473430c6a74a17b60f67b87836439be1bb0cfff79de9c7",
    ),
    (
        "stage-b-execution/1.5",
        EXECUTION_DIR / "freeze-1.5.json",
        "sha256:b08ea2b5754a1d49197300b588f8753ef1fa680391cfd567018eb2be9848dc8f",
    ),
)
#: The freeze a live cell or calibration is authorized against. Moving this pointer is a new
#: version, never a rewrite: the previous file keeps its bytes and its place in the chain.
#: 1.6 is prepared but not written: the writer, the chain and the supersedes rule are in place, and
#: no live cell can start until the file exists (``active_execution_freeze_present``).
FREEZE_VERSION = "stage-b-execution/1.6"
FREEZE_PATH = EXECUTION_DIR / "freeze-1.6.json"

SMOKE_EVIDENCE_DIR = EXECUTION_DIR.parents[1] / "stage_b_smoke"
#: The one E2F provisioning attempt. It ran once and its claim is spent, so the path is a record,
#: never a default: a missing file stays NOT_RUN and never becomes a pass.
PROVISIONING_EVIDENCE_PATH = (
    SMOKE_EVIDENCE_DIR
    / "e2f-provisioning"
    / "attempts"
    / "attempt-a22c2b7650af7e33"
    / "provisioning-result.json"
)
PROVISIONING_REVIEW_PATH = (
    SMOKE_EVIDENCE_DIR / "e2f-provisioning" / "reviews" / "s03-evidence-review.json"
)
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
    "smoke.py",
)


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_text(text: str) -> str:
    return digest_bytes(text.encode("utf-8"))


def content_digest(path: Path) -> str:
    """The digest of a freeze's content, independent of CRLF/LF rendering by ``core.autocrlf``."""

    return digest_bytes(path.read_bytes().replace(b"\r\n", b"\n"))


def version_suffix(version: str) -> str:
    """``stage-b-execution/1.4`` -> ``1.4``."""

    return version.rsplit("/", 1)[-1]


def archive_path(version: str) -> Path:
    """The archived duplicate of a freeze. A duplicate is written once and never rewritten."""

    return ARCHIVE_DIR / f"freeze-{version_suffix(version)}.json"


def frozen_chain_problems() -> list[str]:
    """Re-derive every spent freeze from bytes. A missing or moved file is a hard failure."""

    problems: list[str] = []
    for version, path, expected in FROZEN_CHAIN:
        if not path.is_file():
            problems.append(f"{version}: missing {path.name}")
            continue
        actual = content_digest(path)
        if actual != expected:
            problems.append(f"{version}: {path.name} digest {actual} != {expected}")
    return problems


def freeze_history_problems(payload: Mapping[str, Any]) -> list[str]:
    """The active freeze must name the chain it supersedes, with the real historical digests."""

    problems: list[str] = []
    if payload.get("freeze_version") != FREEZE_VERSION:
        problems.append(f"freeze_version={payload.get('freeze_version')!r} != {FREEZE_VERSION!r}")
    previous_version, previous_path, previous_digest = FROZEN_CHAIN[-1]
    supersedes = payload.get("supersedes") or {}
    if supersedes.get("version") != previous_version:
        problems.append(f"supersedes.version={supersedes.get('version')!r} != {previous_version!r}")
    if supersedes.get("content_digest") != previous_digest:
        problems.append(
            f"supersedes.content_digest={supersedes.get('content_digest')!r} != {previous_digest!r}"
        )
    if str(supersedes.get("live_path") or "") != str(previous_path):
        problems.append(f"supersedes.live_path={supersedes.get('live_path')!r} != {previous_path}")
    history = {
        str(item.get("version")): item.get("content_digest")
        for item in payload.get("history") or ()
    }
    for version, _path, digest in FROZEN_CHAIN:
        if history.get(version) != digest:
            problems.append(f"history[{version}]={history.get(version)!r} != {digest!r}")
    return problems


def normalise_line_endings(data: bytes) -> bytes:
    """A worktree's CRLF rendering is not a content difference.

    ``core.autocrlf`` rewrites files on checkout and on ``git add``, so a raw-byte digest of the
    harness changes without a single character of code changing (measured: the same commit produced
    two digests across a ``git add``). Identity is content, not carriage returns.
    """

    return data.replace(b"\r\n", b"\n")


def source_file_digests(contents: Mapping[str, bytes]) -> dict[str, str]:
    """Per-file digests after line-ending normalisation. One place, so nothing can diverge."""

    return {
        name: digest_bytes(normalise_line_endings(data)) for name, data in contents.items()
    }


def source_files_digest(contents: Mapping[str, bytes]) -> str:
    """The identity digest of a ``name -> bytes`` mapping."""

    payload = json.dumps(source_file_digests(contents), sort_keys=True, separators=(",", ":"))
    return digest_text(payload)


def source_identity() -> dict[str, Any]:
    """Digest the execution harness itself, independently of the frozen analysis harness."""

    contents = {name: (EXECUTION_DIR / name).read_bytes() for name in SOURCE_FILES}
    files = source_file_digests(contents)
    return {
        "root": str(EXECUTION_DIR),
        "file_count": len(files),
        "files": files,
        "digest": source_files_digest(contents),
        "line_endings": "crlf-normalised",
    }


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_digest(payload: Mapping[str, Any]) -> str:
    return digest_text(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def active_freeze_document() -> dict[str, Any]:
    """The freeze a live run is authorized against.

    Until 1.5 is written, that is the last spent version: a reader must not crash merely because
    the next freeze is still being reviewed. ``preflight`` decides readiness separately, and a live
    cell is refused by ``active_execution_freeze_present`` while the pointer has no file.
    """

    if FREEZE_PATH.is_file():
        return read_json(FREEZE_PATH)
    return read_json(FROZEN_CHAIN[-1][1])


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
    active: dict[str, Any] = read_json(FREEZE_PATH) if FREEZE_PATH.is_file() else {}
    freeze_source = (active.get("source") or {}).get("digest")
    # A live cell is authorized against the exact harness bytes it will run. Missing, null, empty
    # and stale digests all fail closed: "no recorded binding" is never a pass. Only the offline
    # suite may run before the next freeze is written, because that run produces the report the
    # freeze is built from, so absence is not binding there.
    bound = isinstance(freeze_source, str) and bool(freeze_source)
    source_ok = (bound and freeze_source == source["digest"]) if require_offline_report else True
    checks.append(
        _check(
            source_ok,
            "runner_source_digest_matches_freeze",
            f"current={source['digest']} frozen={freeze_source}"
            + ("" if require_offline_report else " (not binding offline)"),
        )
    )
    # A live cell needs a written, current freeze. Offline this is the state a new freeze is
    # written from, so absence is expected rather than a failure.
    checks.append(
        _check(
            bool(active) or not require_offline_report,
            "active_execution_freeze_present",
            f"{FREEZE_PATH.name} ({FREEZE_VERSION})"
            + ("" if require_offline_report else " (not binding offline)"),
        )
    )
    chain_problems = frozen_chain_problems()
    checks.append(
        _check(
            not chain_problems,
            "execution_freeze_chain_intact",
            "; ".join(chain_problems) or f"{len(FROZEN_CHAIN)} frozen versions",
        )
    )
    if active:
        history_problems = freeze_history_problems(active)
        checks.append(
            _check(
                not history_problems,
                "execution_freeze_supersedes_history",
                "; ".join(history_problems) or FREEZE_VERSION,
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

    from .provision import sandbox_resource_audit

    resources = sandbox_resource_audit(document)
    detail = str(resources["config"])
    if not resources["ok"]:
        detail += " | " + "; ".join(resources["problems"])
    checks.append(_check(resources["ok"], "sandbox_resource_configuration", detail))

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

    # The archive is a duplicate of each spent freeze, so it must never disagree with the live file.
    for version, _live_path, expected in FROZEN_CHAIN:
        archived = archive_path(version)
        archived_ok = not archived.is_file() or (
            archived.is_file() and content_digest(archived) == expected
        )
        checks.append(
            _check(
                archived_ok,
                f"freeze_{version_suffix(version).replace('.', '_')}_archived",
                str(archived),
            )
        )

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
    """Write the next versioned execution freeze. No earlier freeze is ever rewritten."""

    if FREEZE_PATH.exists():
        raise RuntimeError(f"{FREEZE_VERSION} already exists and is never rewritten: {FREEZE_PATH}")
    problems = frozen_chain_problems()
    if problems:
        raise RuntimeError("the execution freeze chain is broken: " + "; ".join(problems))

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = []
    for version, live_path, expected in FROZEN_CHAIN:
        archived = archive_path(version)
        if not archived.is_file():
            shutil.copy2(live_path, archived)
        if content_digest(archived) != expected:
            raise RuntimeError(f"the archived {version} freeze does not match the live copy")
        history.append(
            {
                "version": version,
                "archive_path": str(archived),
                # Both are recorded: the bytes this worktree rendered, and the rendering-free pin.
                "digest": digest_bytes(live_path.read_bytes()),
                "content_digest": expected,
            }
        )

    previous_version, previous_path, previous_digest = FROZEN_CHAIN[-1]
    document = {
        "stage": "B-E2F-PROVISIONING-GATE",
        "freeze_version": FREEZE_VERSION,
        "supersedes": {
            "version": previous_version,
            "live_path": str(previous_path),
            "archive_path": str(archive_path(previous_version)),
            "digest": digest_bytes(previous_path.read_bytes()),
            "content_digest": previous_digest,
        },
        "history": history,
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
            # Versioned with the adapter, never a literal: a freeze must name the view semantics
            # that produced its evidence.
            "execution_analysis_version": _execution_analysis_version(),
            "e1_analyser_unchanged": "stage-b-reconstruct/1.1",
        },
        "calibration": {
            "task": "workgrid",
            "cell_id": "CALIBRATION.workgrid.c3",
            "previous_attempts": ["CALIBRATION.workgrid.c1", "CALIBRATION.workgrid.c2"],
            "in_matrix": False,
            "status": "PREPARED_NOT_RUN",
            "attempts_authorised_total": 2,
            "authorised_this_stage": 0,
            "reason_not_run": "no attempt is authorised for this harness version",
            "next_identity_required": False,
            "identity": "experiments/stage_s2_prompt_v21/freeze.json",
            "authorization": "PENDING_OWNER_APPROVAL",
            "authorization_path": "experiments/stage_b_calibration/authorization-c3.json",
        },
        "provisioning_gate": _provisioning_gate_summary(),
        "smoke_test": _smoke_summary(),
    }
    FREEZE_PATH.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return document


def _execution_analysis_version() -> str:
    """The adapter owns the view version; importing it here avoids a literal that can drift."""

    from .adapter import ANALYSIS_VERSION

    return ANALYSIS_VERSION


def _provisioning_gate_summary() -> dict[str, Any]:
    """Record the single E2F attempt as it stands. A missing record is NOT_RUN, never a pass."""

    summary: dict[str, Any] = {
        "status": "NOT_RUN",
        "verdict": None,
        "evidence": str(PROVISIONING_EVIDENCE_PATH),
        "digest": None,
        "attempt": None,
        "review": "ABSENT",
        "process_exit_code": "UNOBSERVED",
        "retry": "FORBIDDEN",
    }
    if PROVISIONING_EVIDENCE_PATH.is_file():
        payload = read_json(PROVISIONING_EVIDENCE_PATH)
        cleanup = payload.get("cleanup") or {}
        summary.update(
            {
                "status": payload.get("status"),
                "verdict": payload.get("verdict"),
                "digest": digest_bytes(PROVISIONING_EVIDENCE_PATH.read_bytes()),
                "attempt": payload.get("attempt"),
                "sandbox_created": bool((payload.get("create") or {}).get("attempted")),
                "sandbox_destroyed": bool(cleanup.get("destroyed")),
                "residual": cleanup.get("residual"),
                "checks_true": sum(
                    1 for value in (payload.get("checks") or {}).values() if value is True
                ),
                "checks_total": len(payload.get("checks") or {}),
                "unconfirmed": list(payload.get("unconfirmed") or ()),
                "model_requests": payload.get("model_requests"),
                "calibration_attempts": payload.get("calibration_attempts"),
                "benchmark_cells": payload.get("benchmark_cells"),
            }
        )
    if PROVISIONING_REVIEW_PATH.is_file():
        record = read_json(PROVISIONING_REVIEW_PATH)
        review = record.get("review") or record
        summary["review"] = review.get("review_disposition")
        summary["process_exit_code"] = review.get("process_exit_code")
        summary["review_digest"] = digest_bytes(PROVISIONING_REVIEW_PATH.read_bytes())
        summary["review_path"] = str(PROVISIONING_REVIEW_PATH)
    return summary


def _smoke_summary() -> dict[str, Any]:
    """Read the last smoke result if one exists. A missing result is NOT_RUN, never PASS."""

    path = SMOKE_EVIDENCE_DIR / "sandbox-smoke" / "smoke-result.json"
    if not path.is_file():
        return {
            "status": "NOT_RUN",
            "verdict": "SANDBOX_SMOKE_BLOCKED",
            "evidence": str(path),
            "digest": None,
        }
    payload = read_json(path)
    relevant = {
        key: payload.get(key)
        for key in ("status", "verdict", "model_requests", "calibration_attempts", "checks")
    }
    return {
        **relevant,
        "evidence": str(path),
        "digest": digest_bytes(path.read_bytes()),
        "sandbox_created": bool((payload.get("create") or {}).get("ok")),
        "sandbox_destroyed": bool((payload.get("cleanup") or {}).get("destroyed")),
    }


def write_preflight(result: Mapping[str, Any]) -> Path:
    PREFLIGHT_PATH.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return PREFLIGHT_PATH
