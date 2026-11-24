"""Raw evidence collection and read-only evidence recovery for one cell.

The private raw bundle is immutable after collection. Derived views and the redacted publication are
built from it, never the other way round. A missing artefact is recorded as a gap, never guessed.
"""

from __future__ import annotations

import base64
import json
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .. import protocol, reconstruct, schema
from ..tasks import BY_ID
from . import adapter, provision
from .models import CellIdentity, EvidenceAlreadyExists

RAW_FILES = (
    "session.json",
    "tool_conversations.json",
    "workspace.patch",
    "tool_state.json",
    "workers.json",
    "PRIVATE_PAYLOAD_NOTICE.txt",
)
RAW_DIR = "_raw"
MANIFEST = "evidence_manifest.json"
ANALYSIS = "analysis.json"


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def digest_bytes(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _copy(source: Path, target: Path, *, overwrite: bool = False) -> str | None:
    if not source.exists():
        return None
    if target.exists() and not overwrite:
        raise EvidenceAlreadyExists(f"refusing to overwrite evidence: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target.name


def collect_raw(run_dir: Path, *, bundle: Path, attempt_path: Path | None) -> list[str]:
    """Copy the immutable raw bundle. Returns the names that were absent."""

    if bundle.is_dir() and any(bundle.iterdir()):
        raise EvidenceAlreadyExists(f"evidence bundle already exists: {bundle}")
    raw = bundle / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in RAW_FILES:
        if _copy(run_dir / name, raw / name) is None:
            missing.append(name)

    trajectory_source = run_dir / "trajectory"
    if trajectory_source.is_dir():
        shutil.copytree(trajectory_source, raw / "trajectory")
    else:
        missing.append("trajectory/")

    transcripts = sorted((raw / "trajectory").glob("*/transcript.jsonl"))
    if transcripts:
        shutil.copy2(transcripts[0], raw / "transcript.jsonl")
    elif (run_dir / "transcript.jsonl").is_file():
        shutil.copy2(run_dir / "transcript.jsonl", raw / "transcript.jsonl")
    else:
        missing.append("transcript.jsonl")

    for name in ("handoffs", "private"):
        source = run_dir / name
        if source.is_dir():
            shutil.copytree(source, raw / name)

    if attempt_path is not None and attempt_path.is_file():
        shutil.copy2(attempt_path, raw / "attempt-1.json")
    else:
        missing.append("attempt-1.json")

    # A published copy is made once. Scrubbing only ever touches these copies.
    for path in raw.rglob("*"):
        if path.is_file():
            target = bundle / path.relative_to(raw)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return missing


def restore_published(bundle: Path) -> list[str]:
    """Restore missing published copies from raw. Never modifies raw or existing published files."""

    raw = bundle / RAW_DIR
    if not raw.is_dir():
        return []
    restored: list[str] = []
    for path in raw.rglob("*"):
        if not path.is_file():
            continue
        target = bundle / path.relative_to(raw)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            restored.append(str(target.relative_to(bundle)).replace("\\", "/"))
    return restored


def derive(
    session: Mapping[str, Any], *, bundle: Path, host_oracle: Mapping[str, Any] | None
) -> list[str]:
    """Write Host-side views. Host Oracle absence is UNKNOWN, never a session-verification copy."""

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
        # A Direct run has no proposal, so there is no decision to collect.
        decisions_payload = {
            "decisions": [],
            "status": "NOT_APPLICABLE",
            "reason": "no delegation proposal in this run",
        }
        _write_json(bundle / "router_decisions.json", decisions_payload)
    if batches:
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
        oracle = {
            "task_id": None,
            "source": "UNKNOWN",
            "ok": None,
            "status": "UNKNOWN",
            "executed_after_main_stops": None,
            "reason": "host oracle result was not collected",
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
    """Regenerate the v1.1 record plus the versioned execution overlay.

    A run that died before the model loop has no conversation and no workspace patch. That is an
    explained absence, not a corrupt bundle: the analysis is written as INCOMPLETE with the stage
    and the missing artefacts, and it never pretends the run reached the agent.
    """

    if not (bundle / "session.json").is_file():
        raise RuntimeError(f"cannot analyse a bundle without session.json: {bundle}")
    document = document or protocol.read_protocol()
    session = json.loads((bundle / "session.json").read_text(encoding="utf-8"))
    gaps = list(evidence_gaps)
    oracle_path = bundle / "oracle.json"
    oracle = json.loads(oracle_path.read_text(encoding="utf-8")) if oracle_path.is_file() else None

    conversation = bundle / "tool_conversations.json"
    patch = bundle / "workspace.patch"
    if not conversation.is_file():
        stage = adapter.execution_stage(session)
        not_applicable = [
            name
            for name, present in (
                ("tool_conversations.json", False),
                ("workspace.patch", patch.is_file()),
            )
            if not present
        ]
        gaps.append(
            f"no agent conversation was produced: the run ended in {stage} before the model loop"
        )
        view = adapter.build_view(session, oracle=oracle, gaps=gaps)
        payload = {
            "schema_version": schema.SCHEMA_VERSION,
            "analyzer_version": reconstruct.ANALYZER_VERSION,
            "reconstruct_version": reconstruct.ANALYZER_VERSION,
            "execution_analysis_version": adapter.ANALYSIS_VERSION,
            "harness_source_digest": source_digest,
            "protocol_version": document["protocol_version"],
            "cell": cell.to_dict(),
            "generated_at": datetime.now(UTC).isoformat(),
            "status": "INCOMPLETE",
            "degraded": {
                "reason": "the run ended before the agent loop produced a conversation",
                "stage": stage,
                "session_status": session.get("status"),
                "session_failure": session.get("failure"),
                "not_applicable": not_applicable,
                "missing_raw": [str(conversation.name)] if not conversation.is_file() else [],
            },
            "consistency_checks": {
                "schema_problems": ["record not reconstructed: pre-agent failure"],
                "oracle_agreement": view["goal_correctness"]["oracle_agreement"],
            },
            "evidence_gaps": gaps,
            "valid": False,
            "record": None,
            "execution_view": view,
        }
        _write_json(bundle / ANALYSIS, payload)
        return payload

    record = reconstruct.reconstruct(bundle)
    record = reconstruct.annotate(
        record, task_id=cell.task_id, arm_id=cell.arm_id, repetition=cell.repetition
    )
    problems = schema.validate_record(record)
    gaps.extend(record["consistency"].get("gaps") or [])
    # The raw session is the only place the sandbox lifecycle and the session status survive.
    view = adapter.build_view(record, oracle=oracle, gaps=gaps, session=session)
    record["execution_view"] = view
    payload = {
        "schema_version": schema.SCHEMA_VERSION,
        "analyzer_version": reconstruct.ANALYZER_VERSION,
        "reconstruct_version": reconstruct.ANALYZER_VERSION,
        "execution_analysis_version": adapter.ANALYSIS_VERSION,
        "harness_source_digest": source_digest,
        "protocol_version": document["protocol_version"],
        "cell": cell.to_dict(),
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "COMPLETE" if not problems else "INVALID",
        "consistency_checks": {
            "schema_problems": problems,
            "cache_split_state": record["consistency"].get("cache_split_state"),
            "usage_identity": record["usage"].get("sum_matches_system_total"),
            "transcript_agrees": record["consistency"].get("transcript_agrees"),
            "oracle_agreement": view["goal_correctness"]["oracle_agreement"],
        },
        "evidence_gaps": gaps,
        "valid": not problems,
        "record": record,
    }
    _write_json(bundle / ANALYSIS, payload)
    return payload


def _oracle_texts(task_id: str | None) -> list[str]:
    """Every textual form of this cell's hidden oracle that must not reach a published copy.

    The dynamic acceptance command embeds ``base64(oracle_source)``, so redacting the plaintext
    alone left the hidden program recoverable from every published ``session.json``. The encoded
    form is therefore a secret in its own right. ``task_id=None`` keeps the pre-1.2 behaviour and
    covers all six benchmark tasks.
    """

    sources: list[str] = []
    if task_id is None:
        for spec in BY_ID.values():
            sources.append(spec.oracle_source)
            sources.extend(spec.reference.values())
    elif task_id in BY_ID:
        spec = BY_ID[task_id]
        sources.append(spec.oracle_source)
        sources.extend(spec.reference.values())
    else:
        # A calibration cell runs the S2 contract task, which is not in the benchmark matrix.
        from experiments.stage_s2_prompt_v21 import s2_contract

        sources.append(s2_contract.HIDDEN_PROGRAM)

    texts: list[str] = []
    for text in sources:
        if not text or len(text) <= 40:
            continue
        texts.append(text)
        texts.append(json.dumps(text)[1:-1])
        texts.append(base64.b64encode(text.encode("utf-8")).decode("ascii"))
    return texts


def _relative(path: Path, bundle: Path) -> str:
    return str(path.relative_to(bundle)).replace("\\", "/")


def scrub_evidence(bundle: Path, *, task_id: str | None = None) -> list[str]:
    """Redact the *published* copies only. The private raw bundle is never written.

    Every published file that decodes as UTF-8 is considered, whatever its extension: an oracle
    copied into ``notes.md`` or an extension-less file must not survive because of its name. Files
    that do not decode are left byte-identical here; the publication audit then refuses them unless
    it can prove they are clean.
    """

    secrets = _oracle_texts(task_id)
    redacted: list[str] = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if RAW_DIR in path.relative_to(bundle).parts:
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        cleaned = original
        for secret in secrets:
            if secret in cleaned:
                cleaned = cleaned.replace(secret, "[REDACTED_HOST_ORACLE]")
        if cleaned != original:
            path.write_text(cleaned, encoding="utf-8")
            redacted.append(_relative(path, bundle))
    return redacted


def published_files(bundle: Path) -> list[Path]:
    """The candidate publish set: the public copies only, never the raw evidence.

    This is deliberately a candidate set. Being listed here does not make a file publishable; see
    :func:`publication_audit`, which examines every candidate and refuses what it cannot prove safe.
    """

    return [
        path
        for path in sorted(bundle.rglob("*"))
        if (path.is_file() or path.is_symlink()) and path.relative_to(bundle).parts[0] != RAW_DIR
    ]


def publication_audit(bundle: Path, *, task_id: str | None = None) -> dict[str, Any]:
    """Publication-time acceptance, run after the scrub and before the manifest.

    The publish set is the scrubbed public copies: the raw bundle is not a candidate, and no
    candidate is published without being examined. A candidate is refused when it cannot be read,
    when it is a symlink, when it resolves outside the bundle or back into ``_raw``, when it
    contains NUL bytes, or when it is not valid UTF-8: a file this pipeline cannot decode is a file
    whose cleanliness cannot be proven, so it never reaches ``ok``. A refusal is recorded and makes
    the bundle non-publishable; nothing is deleted or rewritten here.
    """

    problems: list[str] = []
    root = bundle.resolve()
    raw_root = (bundle / RAW_DIR).resolve()
    published = published_files(bundle)
    raw_dir = bundle / RAW_DIR
    raw_files = [path for path in raw_dir.rglob("*") if path.is_file()] if raw_dir.is_dir() else []

    leaked_into_published = [
        _relative(path, bundle)
        for path in published
        if _relative(path, bundle).startswith(f"{RAW_DIR}/")
    ]
    if leaked_into_published:
        problems.append(f"raw files in the published set: {', '.join(leaked_into_published)}")

    secrets = [secret.encode("utf-8") for secret in _oracle_texts(task_id)]
    scanned = 0
    unscannable: list[str] = []
    symlinks: list[str] = []
    for path in published:
        relative = _relative(path, bundle)
        if path.is_symlink():
            symlinks.append(relative)
            problems.append(f"symlink in the evidence bundle: {relative}")
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            problems.append(f"published file escapes the bundle: {relative}")
            continue
        if resolved.is_relative_to(raw_root):
            problems.append(f"published file resolves into the raw bundle: {relative}")
            continue
        try:
            data = path.read_bytes()
        except OSError as error:
            unscannable.append(relative)
            problems.append(f"published file cannot be read ({error}): {relative}")
            continue
        if b"\x00" in data:
            unscannable.append(relative)
            problems.append(
                "published file is binary or in an unknown encoding and cannot be proven clean: "
                + relative
            )
            continue
        # A file must be text this pipeline produced. Undecodable bytes without NUL (a truncated
        # sequence, a latin-1 copy, a binary blob that happens to avoid 0x00) cannot be proven free
        # of oracle material, so it is refused rather than byte-searched and passed.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            unscannable.append(relative)
            problems.append(
                f"published file is not valid UTF-8 ({error.reason} at byte {error.start}) and "
                f"cannot be proven clean: {relative}"
            )
            continue
        scanned += 1
        for secret in secrets:
            if secret in data:
                problems.append(f"hidden oracle material in a published file: {relative}")
                break

    statistics_ok = False
    analysis_path = bundle / ANALYSIS
    if not analysis_path.is_file():
        problems.append("analysis.json is missing from the published set")
    else:
        try:
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(f"published analysis.json does not parse: {error}")
        else:
            record = analysis.get("record") or {}
            usage = record.get("usage") or {}
            statistics_ok = bool(
                analysis.get("status")
                and record.get("execution_view")
                and usage
                and usage.get("sum_matches_system_total") is not False
            )
            if not statistics_ok:
                problems.append("the published analysis cannot back the required statistics")

    for name in ("session.json", "provider_usage.json", "public_tests.json", "oracle.json"):
        path = bundle / name
        if not path.is_file():
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            problems.append(f"published {name} does not parse after redaction: {error}")

    every_candidate_examined = scanned + len(unscannable) + len(symlinks) == len(published)
    if not every_candidate_examined:
        problems.append("a candidate publish file was neither scanned nor refused")

    return {
        "ok": not problems,
        "problems": problems,
        "published_files": len(published),
        "raw_files": len(raw_files),
        "scanned_files": scanned,
        "unscannable_files": unscannable,
        "symlinks": len(symlinks),
        "every_candidate_examined": every_candidate_examined,
        "raw_dir_published": bool(leaked_into_published),
        "statistics_from_published": statistics_ok,
        "policy": (
            "the publish set is the scrubbed public copies only; the raw bundle is never a "
            "candidate, every candidate is examined, and anything that cannot be proven clean is "
            "refused"
        ),
    }


def manifest(
    bundle: Path,
    *,
    redactions: tuple[str, ...] = (),
    publication: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    published: dict[str, str] = {}
    raw: dict[str, str] = {}
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        digest = digest_bytes(path.read_bytes())
        if relative.parts[0] == RAW_DIR:
            raw[str(relative.relative_to(RAW_DIR)).replace("\\", "/")] = digest
        else:
            published[str(relative).replace("\\", "/")] = digest
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "files": published,
        "file_count": len(published),
        "raw_digests": raw,
        "raw_file_count": len(raw),
        "raw_dir": RAW_DIR,
        "immutable_raw": True,
        "redactions": list(redactions),
        "publication": dict(publication or {}),
        "contract": provision.evidence_contract(),
    }
    _write_json(bundle / MANIFEST, payload)
    return payload


def not_applicable(bundle: Path) -> list[str]:
    """Artefacts a pre-agent failure cannot produce. They are explained, never synthesised."""

    if not (bundle / "session.json").is_file():
        return []
    session = json.loads((bundle / "session.json").read_text(encoding="utf-8"))
    if adapter.execution_stage(session) != "PRE_AGENT":
        return []
    return [
        name
        for name in ("tool_conversations.json", "workspace.patch")
        if not (bundle / name).is_file()
    ]


def completeness(bundle: Path) -> list[str]:
    explained = set(not_applicable(bundle))
    missing: list[str] = []
    for name in provision.evidence_contract()["required"]:
        if name in explained:
            continue
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
    """Full collection: immutable raw first, Host views second, analysis and manifest last."""

    gaps = collect_raw(run_dir, bundle=bundle, attempt_path=attempt_path)
    session = json.loads((bundle / "session.json").read_text(encoding="utf-8"))
    gaps.extend(derive(session, bundle=bundle, host_oracle=host_oracle))
    analysis = analyze(
        bundle, cell=cell, source_digest=source_digest, evidence_gaps=gaps, document=document
    )
    redactions = tuple(scrub_evidence(bundle, task_id=cell.task_id))
    publication = publication_audit(bundle, task_id=cell.task_id)
    payload = manifest(bundle, redactions=redactions, publication=publication)
    missing = completeness(bundle)
    explained = not_applicable(bundle)
    return {
        "ok": not missing and bool(analysis["valid"]) and bool(publication["ok"]),
        "status": analysis.get("status", "COMPLETE" if analysis["valid"] else "INVALID"),
        "missing": missing,
        "not_applicable": explained,
        "evidence_gaps": analysis["evidence_gaps"],
        "analysis": analysis,
        "manifest": payload,
        "publication": publication,
        "analysis_path": bundle / ANALYSIS,
    }


def recover_evidence(bundle: Path, *, cell: CellIdentity, source_digest: str) -> dict[str, Any]:
    """Read-only recovery: rebuild derived views from raw evidence, never from a provider."""

    if not bundle.is_dir():
        raise RuntimeError(f"no evidence bundle to recover: {bundle}")
    raw = bundle / RAW_DIR
    source = raw if raw.is_dir() else bundle
    # Only the session is indispensable. A pre-agent failure legitimately has no conversation and
    # no workspace patch; those stay explained absences, not fabricated files.
    if not (source / "session.json").is_file():
        return {
            "ok": False,
            "status": "BLOCKED",
            "missing_raw": ["session.json"],
            "reason": "the session record is missing; nothing may be inferred",
        }
    raw_missing = [
        name
        for name in ("tool_conversations.json", "workspace.patch", "transcript.jsonl")
        if not (source / name).is_file()
    ]

    raw_digests_before = {
        str(path.relative_to(source)).replace("\\", "/"): digest_bytes(path.read_bytes())
        for path in source.rglob("*")
        if path.is_file()
    }
    restored = restore_published(bundle)
    # The existing Host Oracle publication is authoritative; recovery never re-runs it or edits it.
    gaps = list(restored)
    analysis = analyze(bundle, cell=cell, source_digest=source_digest, evidence_gaps=gaps)
    redactions = tuple(scrub_evidence(bundle, task_id=cell.task_id))
    publication = publication_audit(bundle, task_id=cell.task_id)
    payload = manifest(bundle, redactions=redactions, publication=publication)
    raw_digests_after = {
        str(path.relative_to(source)).replace("\\", "/"): digest_bytes(path.read_bytes())
        for path in source.rglob("*")
        if path.is_file()
    }
    missing = completeness(bundle)
    degraded = analysis.get("status") == "INCOMPLETE"
    return {
        "ok": not missing and bool(analysis["valid"]) and bool(publication["ok"]),
        "status": "INCOMPLETE" if (missing or degraded or not publication["ok"]) else "RECOVERED",
        "degraded": degraded,
        "publication": publication,
        "missing": missing,
        "missing_raw_optional": raw_missing,
        "not_applicable": not_applicable(bundle),
        "evidence_gaps": analysis["evidence_gaps"],
        "analysis_path": bundle / ANALYSIS,
        "analysis_version": adapter.ANALYSIS_VERSION,
        "harness_source_digest": source_digest,
        "raw_digests_unchanged": raw_digests_before == raw_digests_after,
        "restored_published": restored,
        "manifest": payload,
    }
