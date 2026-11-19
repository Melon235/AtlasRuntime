"""Materialising the Stage S1 fixture as a clean repository, and identifying it.

One implementation, used by the freeze, the preflight, the offline solvability proof and the live run,
so there is exactly one answer to "which bytes were provisioned".
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import s1_contract as contract


def fixture_manifest() -> dict[str, str]:
    """Relative path -> ``sha256`` of every frozen fixture file."""

    files: dict[str, str] = {}
    for path in sorted(contract.FIXTURE_ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(contract.FIXTURE_ROOT)
        if any(part in contract.FIXTURE_SKIP for part in relative.parts):
            continue
        files[relative.as_posix()] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def fixture_digest() -> str:
    import json

    return contract.digest(json.dumps(fixture_manifest(), sort_keys=True))


def _fresh(destination: Path) -> Path:
    """A path that does not exist yet.

    Nothing here deletes a directory: a provisioner that must first remove its destination cannot be
    run twice safely, and a removal is not part of what this stage proves. A new sibling is used
    instead, so every call gets a pristine tree.
    """

    if not destination.exists():
        return destination
    for index in range(1, 1000):
        candidate = destination.with_name(f"{destination.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find a free scratch path next to {destination}")


def materialize(destination: Path, *, with_git: bool = True) -> tuple[Path, str]:
    """Copy the frozen fixture to a fresh ``destination`` and, optionally, commit it."""

    destination = _fresh(destination)
    destination.mkdir(parents=True)
    for name in fixture_manifest():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(contract.FIXTURE_ROOT / name, target)
    if not with_git:
        return destination, ""
    return destination, init_repository(destination)


def init_repository(root: Path) -> str:
    """Commit whatever is in ``root`` and return the commit id."""

    def run_git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        )
        return result.stdout.strip()

    run_git("init", "-q", "-b", "main")
    run_git("config", "user.email", "stage-s1@atlasruntime.invalid")
    run_git("config", "user.name", "Stage S1 Fixture")
    run_git("add", "-A")
    run_git(
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture: releaseguard without the shared release readiness audit",
    )
    return run_git("rev-parse", "HEAD")


def copy_for_experiment(destination: Path) -> Path:
    """A plain directory copy, no git: what the offline cases mutate."""

    destination = _fresh(destination)
    shutil.copytree(
        contract.FIXTURE_ROOT,
        destination,
        ignore=shutil.ignore_patterns(*contract.FIXTURE_SKIP),
    )
    return destination


__all__ = [
    "copy_for_experiment",
    "fixture_digest",
    "fixture_manifest",
    "init_repository",
    "materialize",
]
