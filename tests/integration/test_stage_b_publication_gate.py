"""Stage B S05 review: the publication gate in front of every collected bundle.

``scrub_evidence`` runs on the published copies only, after the analysis has been derived from the
raw bundle. This module pins the order and the acceptance: raw is never publishable, no textual
form of the hidden oracle survives in a published file, and the scrubbed analysis can still back the
required statistics. Success, failure and recovery paths are all covered, offline.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest
from experiments.stage_benchmark import protocol
from experiments.stage_benchmark.execution import (
    admission,
    collector,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import CellRequest
from experiments.stage_benchmark.tasks import BY_ID


async def _run_cell(root: Path, *, session_kwargs: dict[str, Any] | None = None) -> Any:
    document = protocol.read_protocol()
    cell = provision.cell_from_plan(document, "slugkit.P1.r1")
    kwargs = session_kwargs or {}

    async def scripted(options: Any) -> dict[str, Any]:
        payload = offline.synthetic_session(options, **kwargs)
        offline.write_synthetic_run(options, payload)
        return payload

    request = CellRequest(
        cell=cell,
        runs_root=root / "runs",
        evidence_root=root / "matrix",
        protocol_document=document,
        provider_factory=offline.FakeProvider().build,
        sandbox_factory=lambda _root: offline.FakeProvider(),
        run_fn=scripted,
        host_oracle={
            "source": "fake:host:publication-test",
            "task_id": cell.task_id,
            "ok": True,
            "executed_after_main_stops": True,
        },
    )
    return await runner.execute_cell(request)


def _manifest(bundle: Path) -> dict[str, Any]:
    return json.loads((bundle / "evidence_manifest.json").read_text(encoding="utf-8"))


def test_a_successful_bundle_publishes_only_scrubbed_derived_copies(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None

    payload = _manifest(bundle)
    publication = payload["publication"]
    assert publication["ok"] is True
    assert publication["problems"] == []
    assert publication["raw_dir_published"] is False
    assert publication["statistics_from_published"] is True
    assert publication["raw_files"] > 0
    # The raw digests are still recorded in the manifest, they are just not published files.
    assert payload["raw_digests"]
    assert all(not name.startswith("_raw/") for name in payload["files"])
    assert set(payload["files"]) >= {"analysis.json", "session.json", "oracle.json"}


def test_a_failed_run_publishes_the_same_clean_set(tmp_path: Path) -> None:
    result = asyncio.run(
        _run_cell(
            tmp_path,
            session_kwargs={"status": "failed", "stop": "budget_exceeded", "public": "FAIL"},
        )
    )
    assert result.ok is False
    bundle = result.evidence_dir
    assert bundle is not None
    publication = _manifest(bundle)["publication"]
    assert publication["ok"] is True, publication["problems"]
    assert publication["raw_dir_published"] is False


def test_recovery_rebuilds_the_same_clean_publication(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None

    recovered = collector.recover_evidence(
        bundle, cell=result.cell, source_digest=admission.source_identity()["digest"]
    )
    assert recovered["status"] in {"RECOVERED", "INCOMPLETE"}
    assert recovered["raw_digests_unchanged"] is True
    assert recovered["publication"]["ok"] is True, recovered["publication"]["problems"]
    assert _manifest(bundle)["publication"]["raw_dir_published"] is False


def test_the_gate_reports_a_leak_that_the_scrub_missed(tmp_path: Path) -> None:
    """Negative control: without this the gate could pass by never looking."""

    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None

    secret = base64.b64encode(BY_ID["slugkit"].oracle_source.encode("utf-8")).decode("ascii")
    leaked = bundle / "session.json"
    payload = json.loads(leaked.read_text(encoding="utf-8"))
    payload["verification"] = {"dynamic": {"command": f"run('{secret}')"}}
    leaked.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["ok"] is False
    assert any("hidden oracle material" in problem for problem in audit["problems"])
    assert any("session.json" in problem for problem in audit["problems"])


def test_published_files_never_include_the_raw_directory(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    published = {path.relative_to(bundle).parts[0] for path in collector.published_files(bundle)}
    assert collector.RAW_DIR not in published
    assert (bundle / collector.RAW_DIR).is_dir()


def test_the_scrub_still_runs_before_the_manifest_is_written(tmp_path: Path) -> None:
    """The manifest digests must describe the scrubbed bytes, not the pre-scrub bytes."""

    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    payload = _manifest(bundle)
    for name, digest in payload["files"].items():
        actual = "sha256:" + __import__("hashlib").sha256(
            (bundle / name.replace("/", "\\")).read_bytes()
        ).hexdigest()
        assert digest == actual, name


# --------------------------------------------------------------------------- fail closed


def test_an_unknown_extension_carrying_the_encoded_oracle_is_refused(tmp_path: Path) -> None:
    """A gate that only looks at known extensions reports PASS on exactly this file."""

    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    secret = base64.b64encode(BY_ID["slugkit"].oracle_source.encode("utf-8")).decode("ascii")
    notes = bundle / "notes.md"
    notes.write_text(f"# scratch\n\n{secret}\n", encoding="utf-8")

    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["ok"] is False
    assert any("notes.md" in problem for problem in audit["problems"])
    assert "hidden oracle material" in " ".join(audit["problems"])


def test_the_scrub_covers_a_file_whose_extension_is_unknown(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    plain = BY_ID["slugkit"].oracle_source
    notes = bundle / "notes"
    notes.write_text(f"copied: {plain}\n", encoding="utf-8")

    redacted = collector.scrub_evidence(bundle, task_id="slugkit")
    assert "notes" in redacted
    assert plain not in notes.read_text(encoding="utf-8")
    assert "[REDACTED_HOST_ORACLE]" in notes.read_text(encoding="utf-8")


def test_a_file_that_cannot_be_scanned_is_refused_not_passed(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    blob = bundle / "payload.bin"
    blob.write_bytes(b"\x00\x01\x02binary\x00")

    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["ok"] is False
    assert "payload.bin" in audit["unscannable_files"]
    assert any("cannot be proven clean" in problem for problem in audit["problems"])


def test_a_symlink_in_the_bundle_is_refused(tmp_path: Path, monkeypatch) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self: Path) -> bool:
        if self.name == "session.json" and self.parent == bundle:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["ok"] is False
    assert audit["symlinks"] == 1
    assert any("symlink" in problem for problem in audit["problems"])


def test_a_real_symlink_to_the_raw_bundle_is_refused(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None
    link = bundle / "raw-link.json"
    try:
        link.symlink_to(bundle / collector.RAW_DIR / "session.json")
    except (OSError, NotImplementedError) as error:  # pragma: no cover - host privilege
        pytest.skip(f"this host cannot create symlinks: {error}")

    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["ok"] is False
    assert audit["symlinks"] == 1


def test_every_candidate_is_examined_and_the_set_matches_the_manifest(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    bundle = result.evidence_dir
    assert bundle is not None

    audit = collector.publication_audit(bundle, task_id="slugkit")
    assert audit["every_candidate_examined"] is True
    assert audit["unscannable_files"] == []
    assert audit["symlinks"] == 0
    assert audit["scanned_files"] == audit["published_files"]

    payload = _manifest(bundle)
    candidates = {path.name for path in collector.published_files(bundle)}
    assert {name for name in payload["files"] if "/" not in name} <= candidates
    roots = {path.relative_to(bundle).parts[0] for path in collector.published_files(bundle)}
    assert collector.RAW_DIR not in roots
