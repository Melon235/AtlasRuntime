"""Raw evidence collection and read-only evidence recovery for one cell.

Order matters: raw artefacts are copied first and hashed; derived analysis is written last, from
those raw artefacts only. A missing artefact is recorded as a gap, never filled with a guess.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import protocol, reconstruct, schema
from ..tasks import BY_ID
from . import provision
from .models import CellIdentity, EvidenceAlreadyExists

RAW_FILES = (
    "session.json",
    "tool_conversations.json",
    "workspace.patch",
    "tool_state.json",
    "workers.json",
    "PRIVATE_PAYLOAD_NOTICE.txt",
)
MANIFEST = "evidence_manifest.json"
ANALYSIS = "analysis.json"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def digest_bytes(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _copy(source: Path, target: Path) -> str | None:
    if not source.exists():
        return None
    if target.exists():
        raise EvidenceAlreadyExists(f"refusing to overwrite evidence: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.name


def collect_raw(run_dir: Path, *, bundle: Path, attempt_path: Path | None) -> list[str]:
    """Copy the raw bundle. Returns the names that were absent and therefore not copied."""

    if bundle.is_dir() and any(bundle.iterdir()):
        raise EvidenceAlreadyExists(f"evidence bundle already exists: {bundle}")
    bundle.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in RAW_FILES:
        if _copy(run_dir / name, bundle / name) is None:
            missing.append(name)

    trajectory_source = run_dir / "trajectory"
    if trajectory_source.is_dir():
        shutil.copytree(trajectory_source, bundle / "trajectory")
    else:
        missing.append("trajectory/")

    transcripts = sorted((bundle / "trajectory").glob("*/transcript.jsonl"))
    if transcripts:
        shutil.copy2(transcripts[0], bundle / "transcript.jsonl")
    elif (run_dir / "transcript.jsonl").is_file():
        shutil.copy2(run_dir / "transcript.jsonl", bundle / "transcript.jsonl")
    else:
        missing.append("transcript.jsonl")

    for name in ("handoffs", "private"):
        source = run_dir / name
        if source.is_dir():
            shutil.copytree(source, bundle / name)

    if attempt_path is not None and attempt_path.is_file():
        shutil.copy2(attempt_path, bundle / "attempt-1.json")
    else:
        missing.append("attempt-1.json")
    return missing


def derive(
    session: Mapping[str, Any], *, bundle: Path, host_oracle: Mapping[str, Any] | None
) -> list[str]:
    """Write the host-side views of router, worker, handoff, usage and oracle evidence."""

    gaps: list[str] = []
    delegation = session.get("delegation") or {}
    batches = delegation.get("batches") or []
    if batches:
        decisions = [
            {
                "batch": index,
                "route": (item.get("decision") or {}).get("route"),
                "reason_codes": (item.get("decision") or {}).get("reason_codes"),
                "blocked_gates": (item.get("decision") or {}).get("blocked_gates"),
                "children": (item.get("decision") or {}).get("children"),
            }
            for index, item in enumerate(batches)
        ]
    else:
        decisions = []
        gaps.append("no router decisions on a run with no delegation")
    _write_json(bundle / "router_decisions.json", {"decisions": decisions})

    workers = [
        {
            "batch": index,
            "worker_id": child.get("worker_id"),
            "success": child.get("success"),
            "stop_reason": child.get("stop_reason"),
            "admitted": child.get("admitted"),
        }
        for index, batch in enumerate(batches)
        for child in (batch.get("children") or [])
    ]
    _write_json(bundle / "worker_outcomes.json", {"workers": workers})

    handoff_reads = session.get("handoff_reads") or []
    _write_json(
        bundle / "handoff_references.json",
        {"refs": handoff_reads, "handoff_store": bool((bundle / "handoffs").is_dir())},
    )

    metrics = session.get("metrics") or {}
    usage = {
        "provider": session.get("provider"),
        "model": session.get("model"),
        "main_requests": metrics.get("main_requests"),
        "main_prompt_hit_tokens": metrics.get("main_cache_hit_tokens"),
        "main_prompt_miss_tokens": metrics.get("main_cache_miss_tokens"),
        "child_total_prompt_tokens": metrics.get("child_total_prompt_tokens"),
        "main_completion_tokens": metrics.get("main_completion_tokens"),
        "child_completion_tokens": metrics.get("child_completion_tokens"),
        "system_total_tokens": metrics.get("system_total_tokens"),
    }
    _write_json(bundle / "provider_usage.json", usage)

    public_tests = session.get("public_tests") or {}
    _write_json(bundle / "public_tests.json", public_tests)

    sandbox = session.get("sandbox") or {}
    _write_json(
        bundle / "sandbox_lifecycle.json",
        {
            "sandbox": sandbox,
            "cleanup_failure": (session.get("execution_integrity") or {}).get("sandbox_failure"),
        },
    )

    if host_oracle is not None:
        oracle = dict(host_oracle)
    else:
        verification = session.get("verification") or {}
        dynamic = verification.get("dynamic") or {}
        oracle = {
            "task_id": None,
            "source": "session.verification (no separate host oracle supplied)",
            "executed_after_main_stops": True,
            "ok": dynamic.get("status") == "PASS" or verification.get("verdict") == "PASS",
            "exit_code": dynamic.get("exit_code"),
            "stdout_tail": dynamic.get("stdout_tail", ""),
            "stderr_tail": dynamic.get("stderr_tail", ""),
        }
        gaps.append("host oracle result was not collected separately")
    _write_json(bundle / "oracle.json", oracle)
    return gaps


def analyze(
    bundle: Path,
    *,
    cell: CellIdentity,
    source_digest: str,
    evidence_gaps: list[str],
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Regenerate the v1.1 analysis record from the evidence already in the bundle."""

    if not (bundle / "session.json").is_file():
        raise RuntimeError(f"cannot analyse a bundle without session.json: {bundle}")
    document = document or protocol.read_protocol()
    record = reconstruct.reconstruct(bundle)
    record = reconstruct.annotate(
        record, task_id=cell.task_id, arm_id=cell.arm_id, repetition=cell.repetition
    )
    problems = schema.validate_record(record)
    gaps = list(evidence_gaps)
    gaps.extend(record["consistency"].get("gaps") or [])
    payload = {
        "schema_version": schema.SCHEMA_VERSION,
        "analyzer_version": reconstruct.ANALYZER_VERSION,
        "reconstruct_version": reconstruct.ANALYZER_VERSION,
        "harness_source_digest": source_digest,
        "protocol_version": document["protocol_version"],
        "cell": cell.to_dict(),
        "generated_at": datetime.now(UTC).isoformat(),
        "consistency_checks": {
            "schema_problems": problems,
            "cache_split_state": record["consistency"].get("cache_split_state"),
            "usage_identity": record["usage"].get("sum_matches_system_total"),
            "transcript_agrees": record["consistency"].get("transcript_agrees"),
        },
        "evidence_gaps": gaps,
        "valid": not problems,
        "record": record,
    }
    _write_json(bundle / ANALYSIS, payload)
    return payload


def scrub_evidence(bundle: Path) -> list[str]:
    """Remove oracle/reference text before an evidence bundle is published.

    Nothing here changes a verdict: it only removes secrets that must never leave the Host side.
    """

    secrets = [
        variant
        for spec in BY_ID.values()
        for text in (spec.oracle_source, *spec.reference.values())
        if text and len(text) > 40
        for variant in (text, json.dumps(text)[1:-1])
    ]
    redacted: list[str] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.suffix not in {
            ".json",
            ".jsonl",
            ".txt",
            ".log",
            ".py",
            ".patch",
        }:
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned = original
        for secret in secrets:
            if secret in cleaned:
                cleaned = cleaned.replace(secret, "[REDACTED_HOST_ORACLE]")
        if cleaned != original:
            path.write_text(cleaned, encoding="utf-8")
            redacted.append(str(path.relative_to(bundle)).replace("\\", "/"))
    return redacted


def manifest(bundle: Path, *, redactions: tuple[str, ...] = ()) -> dict[str, Any]:
    digests: dict[str, str] = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(bundle)).replace("\\", "/")] = digest_bytes(
                path.read_bytes()
            )
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "files": digests,
        "file_count": len(digests),
        "redactions": list(redactions),
        "contract": provision.evidence_contract(),
    }
    _write_json(bundle / MANIFEST, payload)
    return payload


def completeness(bundle: Path) -> list[str]:
    missing: list[str] = []
    for name in provision.evidence_contract()["required"]:
        target = bundle / name
        if name.endswith("/"):
            if not target.is_dir() or not any(target.iterdir()):
                missing.append(name)
        elif not target.is_file():
            missing.append(name)
    return missing


def collect(
    *,
    run_dir: Path,
    bundle: Path,
    cell: CellIdentity,
    attempt_path: Path | None,
    source_digest: str,
    host_oracle: Mapping[str, Any] | None = None,
    document: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Full collection: raw first, host views second, analysis and manifest last."""

    gaps = collect_raw(run_dir, bundle=bundle, attempt_path=attempt_path)
    session = json.loads((bundle / "session.json").read_text(encoding="utf-8"))
    gaps.extend(derive(session, bundle=bundle, host_oracle=host_oracle))
    analysis = analyze(
        bundle, cell=cell, source_digest=source_digest, evidence_gaps=gaps, document=document
    )
    redactions = tuple(scrub_evidence(bundle))
    payload = manifest(bundle, redactions=redactions)
    missing = completeness(bundle)
    return {
        "ok": not missing and bool(analysis["valid"]),
        "missing": missing,
        "evidence_gaps": analysis["evidence_gaps"],
        "analysis": analysis,
        "manifest": payload,
        "analysis_path": bundle / ANALYSIS,
    }


def recover_evidence(bundle: Path, *, cell: CellIdentity, source_digest: str) -> dict[str, Any]:
    """Read-only recovery: regenerate derived analysis from existing raw evidence.

    This never starts a sandbox, an agent runner or a provider. It will not create a missing raw
    artefact, and it will not report success when the bundle is incomplete.
    """

    if not bundle.is_dir():
        raise RuntimeError(f"no evidence bundle to recover: {bundle}")
    raw_required = (
        "session.json",
        "tool_conversations.json",
        "workspace.patch",
        "transcript.jsonl",
    )
    raw_missing = [name for name in raw_required if not (bundle / name).is_file()]
    if raw_missing:
        return {
            "ok": False,
            "status": "BLOCKED",
            "missing_raw": raw_missing,
            "reason": "raw evidence is incomplete; nothing may be inferred",
        }
    analysis = analyze(bundle, cell=cell, source_digest=source_digest, evidence_gaps=[])
    redactions = tuple(scrub_evidence(bundle))
    payload = manifest(bundle, redactions=redactions)
    missing = completeness(bundle)
    return {
        "ok": not missing and bool(analysis["valid"]),
        "status": "RECOVERED" if not missing else "INCOMPLETE",
        "missing": missing,
        "evidence_gaps": analysis["evidence_gaps"],
        "analysis_path": bundle / ANALYSIS,
        "manifest": payload,
    }
