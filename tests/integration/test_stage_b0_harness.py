"""Stage B0 harness: identity and one-shot protection, proven offline.

Two defects are covered here:

* the freeze recorded a Git commit that could never equal the branch head (the freeze file is itself
  a commit), so "is the runtime the frozen one?" had no answer — it is now a source-tree digest,
  with Git kept as ancestry-checked provenance;
* ``--freeze`` deleted the scratch directory, attempt marker included, so a second live run could
  be started by wiping the record of the first. The marker is now claimed atomically and every entry
  point refuses once it (or a collected evidence directory) exists.

Nothing here calls a model, builds a provider, or writes into the repository's evidence directory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
B0_DIR = REPO_ROOT / "experiments" / "stage_b0_controlled"
P1B_DIR = REPO_ROOT / "experiments" / "stage_p1b_live"
for entry in (B0_DIR, P1B_DIR):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

# The runner lives outside ``src``; it is imported here exactly as the CLI imports it.
import run_stage_b0 as runner  # noqa: E402  # pyright: ignore[reportMissingImports]


@pytest.fixture()
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


def _freeze_bytes() -> bytes:
    return runner.FREEZE_PATH.read_bytes()


def _ready() -> dict[str, Any]:
    """A READY admission report, so a test can reach the attempt path without a live provider."""

    return {
        "combined_status": "READY",
        "combined_blocking_failures": [],
        "runtime_status": "READY",
        "delegation_mode": "CONTROLLED_DELEGATION",
        "fixture_commit": "0" * 40,
        "runtime": {"checks": [], "blocking_failures": []},
        "stage_checks": [],
        "freeze": {"git": {"git_head": "0" * 40}, "source": {"tree_digest": "sha256:0"}},
    }


# ---------------------------------------------------------------- identity


def test_source_identity_hashes_the_runtime_tree() -> None:
    identity = runner.source_identity()
    assert identity["root"] == "src/atlasos"
    assert identity["file_count"] > 100
    assert identity["tree_digest"].startswith("sha256:")
    assert "app/main_driven.py" in identity["files"]
    # Stable for the same tree, and independent of Git.
    assert runner.source_identity()["tree_digest"] == identity["tree_digest"]


def test_harness_identity_covers_runner_and_contract() -> None:
    identity = runner.harness_identity()
    assert set(identity) == {"b0_contract.py", "run_stage_b0.py"}
    for value in identity.values():
        assert value.startswith("sha256:")


def test_freeze_separates_commit_identity_from_source_identity() -> None:
    """The committed freeze carries both, and the commit it names is still an ancestor."""

    freeze = json.loads(_freeze_bytes().decode("utf-8"))
    assert freeze["source"]["root"] == "src/atlasos"
    assert freeze["source"]["tree_digest"].startswith("sha256:")
    assert freeze["harness"]["run_stage_b0.py"].startswith("sha256:")
    assert freeze["git"]["git_head"]
    assert freeze["identity"]["tool_error_mode"] == "corrected"
    assert runner.is_ancestor(freeze["git"]["git_head"]) is True
    assert runner.is_ancestor("0" * 40) is False


# ---------------------------------------------------------------- one-shot protection


def test_first_attempt_claims_the_marker(scratch: Path) -> None:
    marker = runner.claim_attempt(scratch, {"stage": "B0", "run_id": "test"})
    assert marker.is_file()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["run_id"] == "test"
    assert runner.prior_attempt(scratch) == f"attempt marker exists: {marker}"


def test_second_attempt_is_refused(scratch: Path) -> None:
    runner.claim_attempt(scratch, {"stage": "B0", "run_id": "first"})
    with pytest.raises(runner.AttemptAlreadyStarted):
        runner.claim_attempt(scratch, {"stage": "B0", "run_id": "second"})
    # The first attempt's record is intact and was not replaced.
    marker = runner.attempt_marker(scratch)
    assert json.loads(marker.read_text(encoding="utf-8"))["run_id"] == "first"


def test_prior_attempt_also_sees_collected_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_root = tmp_path / "evidence"
    (evidence_root / "run-1").mkdir(parents=True)
    monkeypatch.setattr(runner, "EVIDENCE_ROOT", evidence_root)
    assert runner.prior_attempt(tmp_path / "scratch") == (
        f"collected evidence exists: {evidence_root / 'run-1'}"
    )


def test_freeze_is_refused_when_an_attempt_exists(scratch: Path) -> None:
    runner.claim_attempt(scratch, {"stage": "B0", "run_id": "first"})
    before = hashlib.sha256(_freeze_bytes()).hexdigest()
    with pytest.raises(SystemExit, match="refusing to freeze"):
        runner.main(["--freeze", "--scratch", str(scratch)])
    assert hashlib.sha256(_freeze_bytes()).hexdigest() == before


def test_run_is_refused_when_an_attempt_exists(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.claim_attempt(scratch, {"stage": "B0", "run_id": "first"})

    async def explode(_options: Any) -> dict[str, Any]:
        raise AssertionError("run_goal must not be reached when an attempt exists")

    monkeypatch.setattr(runner, "run_goal", explode)
    with pytest.raises(SystemExit, match="refusing to run"):
        runner.main(["--run", "--scratch", str(scratch)])


def test_marker_survives_a_failed_run(scratch: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashed attempt still consumes the one allowed attempt."""

    async def explode(_options: Any) -> dict[str, Any]:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(runner, "run_goal", explode)
    monkeypatch.setattr(runner, "evaluate", lambda _options: _ready())
    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(runner.run(scratch))
    assert runner.attempt_marker(scratch).is_file()
    assert runner.prior_attempt(scratch) is not None


def test_blocked_preflight_never_claims_the_marker(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BLOCKED admission must leave the attempt available and call nothing."""

    async def explode(_options: Any) -> dict[str, Any]:
        raise AssertionError("run_goal must not be reached when preflight is BLOCKED")

    monkeypatch.setattr(runner, "run_goal", explode)
    monkeypatch.setattr(
        runner,
        "evaluate",
        lambda _options: {
            "combined_status": "BLOCKED",
            "combined_blocking_failures": ["api_key_present"],
            "fixture_commit": "deadbeef",
        },
    )
    with pytest.raises(SystemExit, match="BLOCKED"):
        asyncio.run(runner.run(scratch))
    assert not runner.attempt_marker(scratch).exists()
    assert runner.prior_attempt(scratch) is None


def test_evidence_directories_are_never_reused(tmp_path: Path) -> None:
    (tmp_path / "run-1").mkdir()
    assert runner.evidence_dir(tmp_path).name == "run-2"
    (tmp_path / "run-2").mkdir()
    assert runner.evidence_dir(tmp_path).name == "run-3"


def test_preflight_creates_no_marker_and_calls_no_model(
    scratch: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(_options: Any) -> dict[str, Any]:
        raise AssertionError("preflight must never call run_goal")

    monkeypatch.setattr(runner, "run_goal", explode)
    report = runner.preflight(scratch)
    assert report["combined_status"] in {"READY", "BLOCKED"}
    assert report["delegation_mode"] == "CONTROLLED_DELEGATION"
    assert not runner.attempt_marker(scratch).exists()
    check_ids = {check["check_id"] for check in report["stage_checks"]}
    assert {
        "runtime_source_frozen",
        "harness_frozen",
        "freeze_commit_is_ancestor",
        "worktree_clean",
        "tool_error_mode_frozen",
        "goal_frozen",
        "single_live_attempt",
    } <= check_ids
    # The preflight is a record, not an attempt.
    assert (scratch / "preflight.json").is_file()
