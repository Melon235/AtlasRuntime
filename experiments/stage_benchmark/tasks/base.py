"""Task specification and offline verification for the Stage B benchmark.

A task is data: an initial repository, a host-only reference solution, at least three wrong variants
the hidden program must reject, a public suite command, and a host-only oracle program. This module
knows how to materialise a task, run its public suite, run its oracle, and prove the whole set
offline — the same shape Stage S1 and S2 used, generalised so six tasks cost one implementation.

Nothing here calls a model, a sandbox or the network.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Injected into every oracle program so the marker never appears in a task module by accident.
ORACLE_OK = "HIDDEN_OK"
ORACLE_FAIL = "HIDDEN_FAIL"

#: Copied into a scratch tree by nobody; listed so a stray ``__pycache__`` never reaches a manifest.
SKIP_PARTS = frozenset({"__pycache__", ".pytest_cache", ".git"})


@dataclass(frozen=True)
class WrongVariant:
    """A plausible wrong implementation: one or two edits to the reference."""

    name: str
    covers: str
    file: str
    edits: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    structure: str
    rationale: str
    goal: str
    write_scope: tuple[str, ...]
    files: Mapping[str, str]
    reference: Mapping[str, str]
    wrong: tuple[WrongVariant, ...]
    compatibility_command: tuple[str, ...]
    target_command: tuple[str, ...]
    oracle_source: str
    expectation: Mapping[str, Any]
    labels: Mapping[str, Any] = field(default_factory=dict)
    notes: str = ""


# --------------------------------------------------------------------------- manifests


def _digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digest_text(text: str) -> str:
    return _digest_bytes(text.encode("utf-8"))


def manifest(spec: TaskSpec) -> dict[str, str]:
    """Per-file digests of the initial fixture, sorted by path."""

    return {path: _digest_text(text) for path, text in sorted(spec.files.items())}


def fixture_digest(spec: TaskSpec) -> str:
    payload = json.dumps(manifest(spec), sort_keys=True, separators=(",", ":"))
    return _digest_text(payload)


def reference_digest(spec: TaskSpec) -> str:
    payload = {path: _digest_text(text) for path, text in sorted(spec.reference.items())}
    return _digest_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def oracle_digest(spec: TaskSpec) -> str:
    return _digest_text(spec.oracle_source)


def goal_digest(spec: TaskSpec) -> str:
    return _digest_text(spec.goal)


def wrong_variants_digest(spec: TaskSpec) -> str:
    payload = [
        {"name": item.name, "file": item.file, "edits": [list(pair) for pair in item.edits]}
        for item in spec.wrong
    ]
    return _digest_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))


# --------------------------------------------------------------------------- materialising


def build(root: Path, spec: TaskSpec) -> Path:
    """Write the initial fixture. Never overwrites: the caller owns the destination."""

    if root.exists():
        raise RuntimeError(f"fixture destination already exists: {root}")
    for path, text in spec.files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return root


def apply_reference(root: Path, spec: TaskSpec) -> None:
    for path, text in spec.reference.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def apply_variant(root: Path, spec: TaskSpec, variant: WrongVariant) -> None:
    """Apply a wrong variant **after** the reference, so each variant is one small regression."""

    target = root / variant.file
    text = target.read_text(encoding="utf-8")
    for old, new in variant.edits:
        if old not in text:
            raise RuntimeError(
                f"variant {variant.name!r} no longer matches {variant.file}: the reference moved"
            )
        text = text.replace(old, new, 1)
    target.write_text(text, encoding="utf-8", newline="\n")


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")


def copy_tree(source: Path, destination: Path) -> Path:
    """A plain copy that cannot delete anything and never carries build artefacts."""

    if destination.exists():
        for index in range(1, 1000):
            candidate = destination.with_name(f"{destination.name}-{index}")
            if not candidate.exists():
                destination = candidate
                break
        else:  # pragma: no cover - a thousand collisions is not a real scenario
            raise RuntimeError(f"cannot find a free scratch path next to {destination}")
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(*sorted(SKIP_PARTS)),
    )
    return destination


# --------------------------------------------------------------------------- running


def run_pytest(root: Path, argv: tuple[str, ...], *, timeout: int = 300) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", *argv],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return {
        "argv": list(argv),
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-2000:],
        "stderr_tail": completed.stderr[-1200:],
    }


def run_oracle(root: Path, spec: TaskSpec, *, timeout: int = 300) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-B", "-c", spec.oracle_source],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = completed.stdout
    return {
        "exit_code": completed.returncode,
        "ok": completed.returncode == 0 and ORACLE_OK in stdout and ORACLE_FAIL not in stdout,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": completed.stderr[-1200:],
    }


def count_outcome(result: dict[str, Any]) -> dict[str, Any]:
    """The one line of pytest output worth recording: ``N passed`` / ``N failed``."""

    counts = {"exit_code": result["exit_code"], "passed": 0, "failed": 0}
    for line in result["stdout_tail"].splitlines():
        tokens = line.strip().replace(",", " ").split()
        for index, token in enumerate(tokens):
            if index == 0 or not tokens[index - 1].isdigit():
                continue
            if token.startswith("passed"):
                counts["passed"] = int(tokens[index - 1])
            elif token.startswith("failed"):
                counts["failed"] = int(tokens[index - 1])
    return counts


# --------------------------------------------------------------------------- offline proof


def verify_solvability(spec: TaskSpec, scratch: Path) -> dict[str, Any]:
    """The F1 suite for one task: baseline, reference, and every wrong variant.

    Allocates fresh scratch directories; deletes nothing.
    """

    scratch.mkdir(parents=True, exist_ok=True)
    if not spec.wrong:
        raise RuntimeError(f"{spec.task_id} declares no wrong variants")

    baseline = build(scratch / "baseline", spec)
    baseline_compat = run_pytest(baseline, spec.compatibility_command)
    baseline_target = run_pytest(baseline, spec.target_command)
    baseline_oracle = run_oracle(baseline, spec)

    reference = build(scratch / "reference", spec)
    apply_reference(reference, spec)
    reference_suite = run_pytest(reference, ("tests",))
    reference_oracle = run_oracle(reference, spec)

    variants: dict[str, Any] = {}
    for variant in spec.wrong:
        tree = copy_tree(reference, scratch / f"variant-{variant.name}")
        apply_variant(tree, spec, variant)
        variants[variant.name] = {
            "covers": variant.covers,
            "oracle": run_oracle(tree, spec),
            "public_suite": count_outcome(run_pytest(tree, ("tests",))),
        }

    baseline_compat_counts = count_outcome(baseline_compat)
    baseline_target_counts = count_outcome(baseline_target)
    reference_counts = count_outcome(reference_suite)

    rejected = {
        name: value["oracle"]["ok"] is False and ORACLE_FAIL in value["oracle"]["stdout_tail"]
        for name, value in variants.items()
    }
    ok = (
        baseline_compat_counts["exit_code"] == 0
        and baseline_target_counts["exit_code"] != 0
        and baseline_oracle["ok"] is False
        and reference_counts["exit_code"] == 0
        and reference_oracle["ok"]
        and all(rejected.values())
    )
    return {
        "task_id": spec.task_id,
        "ok": ok,
        "fixture_digest": fixture_digest(spec),
        "baseline": {
            "compatibility": baseline_compat_counts,
            "target": baseline_target_counts,
            "oracle_rejects": baseline_oracle["ok"] is False,
        },
        "reference": {
            "suite": reference_counts,
            "oracle_passes": reference_oracle["ok"],
        },
        "wrong_variants": {
            name: {
                "covers": value["covers"],
                "rejected_by_oracle": rejected[name],
                "also_caught_by_public_suite": value["public_suite"]["exit_code"] != 0,
            }
            for name, value in variants.items()
        },
        "expectation": dict(spec.expectation),
        "matches_expectation": {
            "compatibility_tests": baseline_compat_counts["passed"]
            == spec.expectation.get("compatibility_tests"),
            "target_tests": baseline_target_counts["failed"]
            == spec.expectation.get("target_tests"),
            "reference_tests": reference_counts["passed"]
            == spec.expectation.get("reference_tests"),
        },
    }
