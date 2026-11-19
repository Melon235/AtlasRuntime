"""Artifact inventory: what a build directory actually contains.

This package knows nothing about release manifests. It answers three questions — what is in the
directory, what is the digest of each file, and is this a path we are willing to publish — and it
answers them by raising typed errors rather than by accumulating findings.
"""

from __future__ import annotations

from .checksums import digests_equal, normalize_digest, sha256_bytes, sha256_file
from .errors import (
    ArtifactError,
    ArtifactPathError,
    ArtifactScanError,
    DigestFormatError,
)
from .path_rules import is_safe_artifact_path, normalize_artifact_path
from .scanner import (
    ArtifactInventory,
    ScannedArtifact,
    SkippedArtifact,
    scan_artifacts,
)

__all__ = [
    "ArtifactError",
    "ArtifactInventory",
    "ArtifactPathError",
    "ArtifactScanError",
    "DigestFormatError",
    "ScannedArtifact",
    "SkippedArtifact",
    "digests_equal",
    "is_safe_artifact_path",
    "normalize_artifact_path",
    "normalize_digest",
    "scan_artifacts",
    "sha256_bytes",
    "sha256_file",
]
