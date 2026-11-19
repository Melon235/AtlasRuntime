"""Reading a release manifest.

The manifest is JSON. This module is the only place that knows its shape, its default values and
which keys are required.

Two boundaries are deliberate:

* **Paths are kept verbatim.** ``ArtifactEntry.path`` is the string the manifest contains. Whether
  that string is a safe artifact path is the artifact layer's question, and answering it here would
  give the project two definitions of the same rule.
* **Values are not judged.** ``release.version`` is a string here; whether it is a *valid* version,
  and whether it is a valid version *for its channel*, is :mod:`releaseguard.metadata.version_rules`.
  ``license.spdx`` is the same: this module reads it, :mod:`~releaseguard.metadata.license_rules`
  decides whether it is allowed.

Unknown top-level keys are ignored, so a manifest produced by a newer tool does not have to be
rewritten before an older audit can read it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ManifestFieldError, ManifestFormatError

#: Applied when the manifest omits the key. Documented, because a default is part of the contract.
DEFAULT_CHANNEL = "stable"
DEFAULT_PYTHON_REQUIREMENT = ">=3.9"
DEFAULT_ARTIFACT_KIND = "binary"


@dataclass(frozen=True)
class ArtifactEntry:
    """One artifact the manifest says will be published."""

    path: str
    sha256: str
    size: int
    kind: str = DEFAULT_ARTIFACT_KIND

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "size": self.size, "kind": self.kind}


@dataclass(frozen=True)
class LicenseInfo:
    """The license a release is published under."""

    spdx: str
    files: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict[str, Any]:
        return {"spdx": self.spdx, "files": list(self.files)}


@dataclass(frozen=True)
class ReleaseManifest:
    """A parsed release manifest. Frozen: the audit reads it, nothing rewrites it."""

    name: str
    version: str
    channel: str
    python_requirement: str
    license: LicenseInfo
    artifacts: tuple[ArtifactEntry, ...]
    #: Where the manifest was read from, for messages. Never used as a path in this module.
    source: str = "<memory>"

    def artifact_paths(self) -> tuple[str, ...]:
        """Declared artifact paths, in manifest order, exactly as written."""

        return tuple(entry.path for entry in self.artifacts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "channel": self.channel,
            "python_requirement": self.python_requirement,
            "license": self.license.as_dict(),
            "artifacts": [entry.as_dict() for entry in self.artifacts],
            "source": self.source,
        }


def parse_manifest(path: str | Path) -> ReleaseManifest:
    """Read and parse the manifest at ``path``.

    Raises:
        ManifestFormatError: the file is not readable UTF-8 JSON with an object at the root.
        ManifestFieldError: the root is an object, but a required field is missing or mistyped.
    """

    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ManifestFormatError(
            f"cannot read the manifest at {manifest_path}: {error}",
            code="manifest_unreadable",
            subject=str(manifest_path),
        ) from error
    return parse_manifest_text(text, source=str(manifest_path))


def parse_manifest_text(text: str, *, source: str = "<text>") -> ReleaseManifest:
    """Parse manifest text. ``source`` only labels the result; it is not opened."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise ManifestFormatError(
            f"the manifest is not valid JSON: {error.msg} at line {error.lineno}",
            code="manifest_not_json",
            subject=source,
        ) from error
    if not isinstance(payload, dict):
        raise ManifestFormatError(
            f"the manifest must be a JSON object, got {type(payload).__name__}",
            code="manifest_not_object",
            subject=source,
        )

    release = _require_mapping(payload, "release")
    license_section = _require_mapping(payload, "license")
    raw_artifacts = payload.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise ManifestFieldError(
            "artifacts must be a list of artifact entries",
            code="field_type",
            subject="artifacts",
        )

    name = _require_text(release, "release.name", subject="release.name")
    version = _require_text(release, "release.version", subject="release.version")
    spdx = _require_text(license_section, "license.spdx", subject="license.spdx")

    return ReleaseManifest(
        name=name,
        version=version,
        channel=_optional_text(
            release, "channel", DEFAULT_CHANNEL, subject="release.channel"
        ),
        python_requirement=_optional_text(
            release,
            "python_requirement",
            DEFAULT_PYTHON_REQUIREMENT,
            subject="release.python_requirement",
        ),
        license=LicenseInfo(
            spdx=spdx,
            files=_optional_text_tuple(license_section, "files", subject="license.files"),
        ),
        artifacts=_artifacts(raw_artifacts),
        source=source,
    )


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if value is None:
        raise ManifestFieldError(
            f"the manifest has no {key!r} section", code="field_missing", subject=key
        )
    if not isinstance(value, dict):
        raise ManifestFieldError(
            f"the {key!r} section must be an object, got {type(value).__name__}",
            code="field_type",
            subject=key,
        )
    return value


def _require_text(section: dict[str, Any], dotted: str, *, subject: str) -> str:
    key = dotted.rsplit(".", 1)[-1]
    value = section.get(key)
    if value is None:
        raise ManifestFieldError(
            f"{dotted} is required", code="field_missing", subject=subject
        )
    if not isinstance(value, str):
        raise ManifestFieldError(
            f"{dotted} must be a string, got {type(value).__name__}",
            code="field_type",
            subject=subject,
        )
    if not value.strip():
        raise ManifestFieldError(
            f"{dotted} must not be empty", code="field_empty", subject=subject
        )
    return value


def _optional_text(section: dict[str, Any], key: str, default: str, *, subject: str) -> str:
    if key not in section or section[key] is None:
        return default
    value = section[key]
    if not isinstance(value, str):
        raise ManifestFieldError(
            f"{subject} must be a string, got {type(value).__name__}",
            code="field_type",
            subject=subject,
        )
    if not value.strip():
        raise ManifestFieldError(
            f"{subject} must not be empty when it is present", code="field_empty", subject=subject
        )
    return value


def _optional_text_tuple(section: dict[str, Any], key: str, *, subject: str) -> tuple[str, ...]:
    if key not in section or section[key] is None:
        return ()
    value = section[key]
    if not isinstance(value, list):
        raise ManifestFieldError(
            f"{subject} must be a list of strings, got {type(value).__name__}",
            code="field_type",
            subject=subject,
        )
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ManifestFieldError(
                f"{subject} must contain only strings, got {type(item).__name__}",
                code="field_type",
                subject=subject,
            )
        if not item.strip():
            raise ManifestFieldError(
                f"{subject} must not contain an empty entry", code="field_empty", subject=subject
            )
        items.append(item)
    return tuple(items)


def _artifacts(raw_artifacts: list[Any]) -> tuple[ArtifactEntry, ...]:
    entries: list[ArtifactEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_artifacts):
        subject = f"artifacts[{index}]"
        if not isinstance(item, dict):
            raise ManifestFieldError(
                f"{subject} must be an object, got {type(item).__name__}",
                code="field_type",
                subject=subject,
            )
        path = _require_text(item, "path", subject=f"{subject}.path")
        sha256 = _require_text(item, "sha256", subject=f"{subject}.sha256")
        size = item.get("size")
        if size is None:
            raise ManifestFieldError(
                f"{subject}.size is required", code="field_missing", subject=f"{subject}.size"
            )
        if isinstance(size, bool) or not isinstance(size, int):
            raise ManifestFieldError(
                f"{subject}.size must be an integer, got {type(size).__name__}",
                code="field_type",
                subject=f"{subject}.size",
            )
        if size < 0:
            raise ManifestFieldError(
                f"{subject}.size must not be negative",
                code="field_value",
                subject=f"{subject}.size",
            )
        if path in seen:
            raise ManifestFieldError(
                f"the artifact {path!r} is declared more than once",
                code="duplicate_artifact",
                subject=path,
            )
        seen.add(path)
        entries.append(
            ArtifactEntry(
                path=path,
                sha256=sha256,
                size=size,
                kind=_optional_text(
                    item, "kind", DEFAULT_ARTIFACT_KIND, subject=f"{subject}.kind"
                ),
            )
        )
    return tuple(entries)


__all__ = [
    "DEFAULT_ARTIFACT_KIND",
    "DEFAULT_CHANNEL",
    "DEFAULT_PYTHON_REQUIREMENT",
    "ArtifactEntry",
    "LicenseInfo",
    "ReleaseManifest",
    "parse_manifest",
    "parse_manifest_text",
]
