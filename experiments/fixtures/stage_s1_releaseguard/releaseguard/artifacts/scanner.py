"""Deterministic inventory of a build directory.

The inventory is the only description of "what is actually there" the audit has. It is therefore
built so that two runs over the same bytes produce the same value: entries are sorted by path, hashes
are the bare lowercase SHA-256, and nothing about the walk order, the machine or the clock survives.

Two kinds of entry are *refused* rather than reported as artifacts:

* a file whose relative path is not a safe artifact path (see
  :func:`releaseguard.artifacts.path_rules.normalize_artifact_path`);
* a file that resolves outside the build directory — a symlink pointing out of the tree, or a path
  that changed under the walk.

A refusal is recorded in :attr:`ArtifactInventory.skipped` with a code, never silently dropped: an
audit that cannot see a file must be able to say so.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .checksums import sha256_file
from .errors import ArtifactPathError, ArtifactScanError
from .path_rules import normalize_artifact_path

#: Directories that are build tooling, not release content.
DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


@dataclass(frozen=True)
class ScannedArtifact:
    """One file found in the build directory."""

    path: str
    size: int
    sha256: str
    is_symlink: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            "is_symlink": self.is_symlink,
        }


@dataclass(frozen=True)
class SkippedArtifact:
    """One file the scanner refused to describe as an artifact."""

    path: str
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


@dataclass(frozen=True)
class ArtifactInventory:
    """The build directory as the audit sees it."""

    root: str
    entries: tuple[ScannedArtifact, ...] = field(default=())
    skipped: tuple[SkippedArtifact, ...] = field(default=())

    def paths(self) -> tuple[str, ...]:
        return tuple(entry.path for entry in self.entries)

    def by_path(self) -> dict[str, ScannedArtifact]:
        return {entry.path: entry for entry in self.entries}

    def as_dict(self) -> dict[str, object]:
        return {
            "root": self.root,
            "entries": [entry.as_dict() for entry in self.entries],
            "skipped": [item.as_dict() for item in self.skipped],
        }


def scan_artifacts(
    root: str | Path, *, exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS
) -> ArtifactInventory:
    """Inventory every publishable file under ``root``.

    Raises:
        ArtifactScanError: ``root`` is not a readable directory (``code`` is
            ``"artifact_root_missing"``).
    """

    base = Path(root)
    if not base.is_dir():
        raise ArtifactScanError(
            f"the artifact root {base} is not a directory",
            code="artifact_root_missing",
            subject=str(root),
        )
    resolved_base = base.resolve()
    entries: list[ScannedArtifact] = []
    skipped: list[SkippedArtifact] = []

    for dirpath, dirnames, filenames in os.walk(str(base), followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in exclusions)
        for name in sorted(filenames):
            if name in exclusions:
                continue
            found = Path(dirpath) / name
            try:
                relative = found.relative_to(base).as_posix()
            except ValueError:  # pragma: no cover - os.walk cannot leave the root
                relative = found.name
            try:
                canonical = normalize_artifact_path(relative)
            except ArtifactPathError as error:
                skipped.append(SkippedArtifact(path=relative, code=error.code, message=str(error)))
                continue

            is_symlink = found.is_symlink()
            try:
                resolved = found.resolve(strict=True)
                resolved.relative_to(resolved_base)
            except ValueError:
                skipped.append(
                    SkippedArtifact(
                        path=canonical,
                        code="symlink_escape",
                        message=f"{canonical} resolves outside {base}",
                    )
                )
                continue
            except OSError as error:
                skipped.append(
                    SkippedArtifact(
                        path=canonical,
                        code="artifact_unreadable",
                        message=f"{canonical} cannot be resolved: {error}",
                    )
                )
                continue

            try:
                size = resolved.stat().st_size
                digest = sha256_file(resolved)
            except OSError as error:
                skipped.append(
                    SkippedArtifact(
                        path=canonical,
                        code="artifact_unreadable",
                        message=f"{canonical} cannot be read: {error}",
                    )
                )
                continue
            except ArtifactScanError as error:
                skipped.append(SkippedArtifact(path=canonical, code=error.code, message=str(error)))
                continue

            entries.append(
                ScannedArtifact(
                    path=canonical, size=size, sha256=digest, is_symlink=is_symlink
                )
            )

    entries.sort(key=lambda entry: entry.path)
    skipped.sort(key=lambda item: (item.path, item.code))
    return ArtifactInventory(
        root=str(root), entries=tuple(entries), skipped=tuple(skipped)
    )


__all__ = [
    "DEFAULT_EXCLUSIONS",
    "ArtifactInventory",
    "ScannedArtifact",
    "SkippedArtifact",
    "scan_artifacts",
]
