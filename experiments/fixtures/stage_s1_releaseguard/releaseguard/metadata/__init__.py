"""Release metadata: parsing a release manifest and applying the release rules.

Nothing here touches the filesystem beyond reading the manifest file, and nothing here knows that a
build directory exists. That separation is what lets the audit layer be the only place where the two
halves meet.
"""

from __future__ import annotations

from .errors import (
    LicenseRuleError,
    ManifestFieldError,
    ManifestFormatError,
    ReleaseMetadataError,
    VersionRuleError,
)
from .license_rules import LicenseFinding, check_license, normalize_spdx
from .manifest_parser import (
    ArtifactEntry,
    LicenseInfo,
    ReleaseManifest,
    parse_manifest,
    parse_manifest_text,
)
from .version_rules import Version, VersionFinding, check_version, compare_versions, parse_version

__all__ = [
    "ArtifactEntry",
    "LicenseFinding",
    "LicenseInfo",
    "LicenseRuleError",
    "ManifestFieldError",
    "ManifestFormatError",
    "ReleaseManifest",
    "ReleaseMetadataError",
    "Version",
    "VersionFinding",
    "VersionRuleError",
    "check_license",
    "check_version",
    "compare_versions",
    "normalize_spdx",
    "parse_manifest",
    "parse_manifest_text",
    "parse_version",
]
