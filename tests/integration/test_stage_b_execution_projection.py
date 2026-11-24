"""Stage B S05: execution-view projection and published-evidence scrubbing.

Two defects are pinned here, both in the host-side evidence layer:

1. ``adapter.build_view`` reads ``sandbox`` and ``status`` off whatever mapping it is handed. On the
   normal path that mapping is the *reconstructed record*, which carries neither key, so the view
   reported ``sandbox_created=false``, ``sandbox_destroyed=false`` and ``session_status=null`` for a
   run that really did create and destroy a sandbox.
2. ``collector.scrub_evidence`` searched only the plaintext oracle sources of the six benchmark
   tasks. The dynamic acceptance command embeds ``base64(oracle_source)``, so the encoded oracle was
   never redacted, and the calibration task was not in the list at all.

Every test is synthetic or offline: no model call, no real sandbox, no benchmark cell.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

from experiments.stage_benchmark import protocol
from experiments.stage_benchmark.execution import (
    adapter,
    collector,
    offline,
    provision,
    runner,
)
from experiments.stage_benchmark.execution.models import CellRequest
from experiments.stage_benchmark.tasks import BY_ID

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- helpers


def _record(**overrides: Any) -> dict[str, Any]:
    """A reconstructed record: it has ``counts`` and ``run.status``, never ``sandbox``."""

    base: dict[str, Any] = {
        "run": {"status": "succeeded", "stop_reason": "success"},
        "counts": {"provider_requests": 12, "tool_calls": 3},
        "verification": {
            "oracle": "PASS",
            "public_test_calls": 1,
            "public_test_statuses": ["PASS"],
            "goal_passed": True,
        },
        "failure": {"primary": "none"},
        "delegation": {},
        "handoff": {},
    }
    base.update(overrides)
    return base


def _session(**overrides: Any) -> dict[str, Any]:
    """A raw session: this is the only place the lifecycle facts exist."""

    base: dict[str, Any] = {
        "status": "succeeded",
        "sandbox": {"created": True, "destroyed": True, "retained": False},
        "metrics": {"main_requests": 12},
        "execution_integrity": {"sandbox_failure": None},
        "failure": None,
    }
    base.update(overrides)
    return base


def _oracle(ok: bool = True, source: str = "host:tests") -> dict[str, Any]:
    return {"source": source, "ok": ok}


def _integrity(view: dict[str, Any]) -> dict[str, Any]:
    return view["execution_integrity"]


# --------------------------------------------------------------------------- 1. real success


def test_success_run_projects_the_lifecycle_from_the_session() -> None:
    view = adapter.build_view(
        _record(), oracle=_oracle(), gaps=[], session=_session()
    )
    integrity = _integrity(view)
    assert integrity["sandbox_created"] is True
    assert integrity["sandbox_destroyed"] is True
    assert integrity["session_status"] == "succeeded"
    assert integrity["stage"] == "AGENT"
    assert integrity["primary_failure"] == "none"


def test_success_run_end_to_end_records_the_sandbox_in_the_analysis(tmp_path: Path) -> None:
    result = asyncio.run(_run_cell(tmp_path))
    analysis = json.loads(
        (result.evidence_dir / "analysis.json").read_text(encoding="utf-8")
    )
    integrity = analysis["record"]["execution_view"]["execution_integrity"]
    assert integrity["sandbox_created"] is True
    assert integrity["sandbox_destroyed"] is True
    assert integrity["session_status"] == "succeeded"


# --------------------------------------------------------------------------- 2. creation failure


def test_sandbox_creation_failure_is_not_reported_as_a_sandbox() -> None:
    session = _session(
        status="failed",
        sandbox={"created": False, "destroyed": False, "error": "create timed out"},
        metrics={"main_requests": 0},
    )
    record = _record(
        failure={"primary": "sandbox provider 'opensandbox' is unavailable"},
        counts={"provider_requests": 0, "tool_calls": 0},
    )
    integrity = _integrity(adapter.build_view(record, oracle=None, gaps=[], session=session))
    assert integrity["sandbox_created"] is False
    assert integrity["sandbox_destroyed"] is False
    assert integrity["session_status"] == "failed"
    assert integrity["stage"] == "PRE_AGENT"
    assert "opensandbox" in integrity["primary_failure"]


# --------------------------------------------------------------------------- 3. cleanup failure


def test_cleanup_failure_is_distinguishable_from_never_created() -> None:
    session = _session(
        status="failed",
        sandbox={"created": True, "destroyed": False, "error": None},
        execution_integrity={"sandbox_failure": "destroy returned 500"},
    )
    record = _record(failure={"primary": "sandbox_cleanup"})
    integrity = _integrity(adapter.build_view(record, oracle=None, gaps=[], session=session))
    assert integrity["sandbox_created"] is True
    assert integrity["sandbox_destroyed"] is False
    assert integrity["session_status"] == "failed"
    assert integrity["stage"] == "AGENT"
    assert integrity["primary_failure"] == "sandbox_cleanup"


# --------------------------------------------------------------------------- 4. pre-agent failure


def test_pre_agent_failure_keeps_the_degraded_path_unchanged() -> None:
    session = _session(
        status="failed",
        sandbox={"created": False, "destroyed": False},
        metrics={"main_requests": 0},
        failure="sandbox provider 'opensandbox' is unavailable: ReadTimeout",
    )
    gaps: list[str] = []
    view = adapter.build_view(session, oracle=None, gaps=gaps)
    integrity = _integrity(view)
    assert integrity["stage"] == "PRE_AGENT"
    assert integrity["sandbox_created"] is False
    assert integrity["session_status"] == "failed"
    assert integrity["primary_failure"].startswith("sandbox provider")
    assert any("host oracle verdict is UNKNOWN" in gap for gap in gaps)


# --------------------------------------------------------------------------- 5. contracts


def test_a_record_that_carries_its_own_lifecycle_is_still_projected() -> None:
    record = _record(
        status="succeeded",
        sandbox={"created": True, "destroyed": True},
    )
    integrity = _integrity(adapter.build_view(record, oracle=_oracle(), gaps=[]))
    assert integrity["sandbox_created"] is True
    assert integrity["sandbox_destroyed"] is True
    assert integrity["session_status"] == "succeeded"


def test_goal_correctness_and_oracle_authority_do_not_depend_on_the_session() -> None:
    without = adapter.build_view(_record(), oracle=_oracle(), gaps=[])
    with_session = adapter.build_view(
        _record(), oracle=_oracle(), gaps=[], session=_session()
    )
    assert without["goal_correctness"] == with_session["goal_correctness"]
    assert without["goal_correctness"]["status"] == "PASS"
    assert without["goal_correctness"]["authoritative_source"] == "host:tests"

    unverified = adapter.build_view(
        _record(), oracle=_oracle(source="session:verification"), gaps=[], session=_session()
    )
    assert unverified["goal_correctness"]["host_oracle"] == "UNKNOWN"
    assert unverified["goal_correctness"]["status"] == "UNKNOWN"


# --------------------------------------------------------------------------- 6. scrubbing


def _bundle_with_secret(root: Path, text: str) -> Path:
    bundle = root / "bundle"
    (bundle / "_raw").mkdir(parents=True)
    payload = json.dumps({"verification": {"dynamic": {"command": text}}}, indent=2)
    (bundle / "_raw" / "session.json").write_text(payload, encoding="utf-8")
    (bundle / "session.json").write_text(payload, encoding="utf-8")
    return bundle


def test_scrub_redacts_the_base64_encoded_oracle(tmp_path: Path) -> None:
    spec = BY_ID["slugkit"]
    encoded = base64.b64encode(spec.oracle_source.encode("utf-8")).decode("ascii")
    bundle = _bundle_with_secret(tmp_path, f"python3 -c \"exec(b64decode('{encoded}'))\"")
    redacted = collector.scrub_evidence(bundle, task_id="slugkit")
    published = (bundle / "session.json").read_text(encoding="utf-8")
    assert "session.json" in redacted
    assert encoded not in published
    assert "[REDACTED_HOST_ORACLE]" in published
    assert (bundle / "_raw" / "session.json").read_text(encoding="utf-8").count(encoded) == 1


def test_scrub_covers_the_calibration_task(tmp_path: Path) -> None:
    from experiments.stage_s2_prompt_v21 import s2_contract

    encoded = s2_contract.HIDDEN_ENCODED
    bundle = _bundle_with_secret(tmp_path, s2_contract.ORACLE_COMMAND)
    redacted = collector.scrub_evidence(bundle, task_id="workgrid")
    published = (bundle / "session.json").read_text(encoding="utf-8")
    assert "session.json" in redacted
    assert encoded not in published
    assert (bundle / "_raw" / "session.json").read_text(encoding="utf-8").count(encoded) == 1


def test_scrub_leaves_a_clean_bundle_untouched(tmp_path: Path) -> None:
    bundle = _bundle_with_secret(tmp_path, "python3 -B -m pytest -q tests")
    before = (bundle / "session.json").read_bytes()
    assert collector.scrub_evidence(bundle, task_id="slugkit") == []
    assert (bundle / "session.json").read_bytes() == before


# --------------------------------------------------------------------------- rig


async def _run_cell(root: Path) -> Any:
    document = protocol.read_protocol()
    cell = provision.cell_from_plan(document, "slugkit.P1.r1")

    async def scripted(options: Any) -> dict[str, Any]:
        payload = offline.synthetic_session(options)
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
            "source": "fake:host:projection-test",
            "task_id": cell.task_id,
            "ok": True,
            "executed_after_main_stops": True,
        },
    )
    return await runner.execute_cell(request)
