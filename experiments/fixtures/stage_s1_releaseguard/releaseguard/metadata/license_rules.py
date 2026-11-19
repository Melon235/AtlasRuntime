"""License identifiers and the allow-list rules.

An identifier is normalized before it is compared, because manifests in the wild carry
``Apache-2.0``, ``apache2`` and ``APACHE 2.0`` and all three mean the same license. The normalized
form is the SPDX identifier, and that is what a report shows.

Two things are deliberately *not* here: reading license files from disk (that is the artifact
layer's job, and the audit decides whether it cares), and deciding whether the release is publishable
in a legal sense. This module only knows the project's allow list.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .errors import LicenseRuleError, Severity
from .manifest_parser import ReleaseManifest

#: Spellings that mean a different string. Keys are compared case-insensitively after whitespace
#: collapsing, so one entry per spelling family is enough.
_ALIASES: dict[str, str] = {
    "apache2": "Apache-2.0",
    "apache 2.0": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "mit": "MIT",
    "mit license": "MIT",
    "bsd": "BSD-3-Clause",
    "bsd3": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "isc": "ISC",
    "mpl2": "MPL-2.0",
    "unlicensed": "UNLICENSED",
}

#: Deprecated SPDX identifiers and their replacements. Using one is a warning, not a failure: the
#: release is still publishable, it is just no longer machine-obvious.
_DEPRECATED: dict[str, str] = {
    "GPL-2.0": "GPL-2.0-only",
    "GPL-3.0": "GPL-3.0-only",
    "LGPL-2.1": "LGPL-2.1-only",
    "LGPL-3.0": "LGPL-3.0-only",
    "AGPL-3.0": "AGPL-3.0-only",
}

#: The one identifier that does not require a bundled license file.
_NO_FILE_REQUIRED = "UNLICENSED"


@dataclass(frozen=True)
class LicenseFinding:
    """One rule violation, in the vocabulary the audit layer reports."""

    code: str
    subject: str
    message: str
    severity: Severity = "error"


def normalize_spdx(text: str) -> str:
    """Normalize an SPDX identifier or a known alias to its canonical SPDX spelling.

    Raises:
        LicenseRuleError: the text is empty, or it is a deprecated identifier whose replacement the
            caller must choose deliberately. Deprecated identifiers are refused *here* and reported
            as a warning by :func:`check_license`, so a caller that normalizes one directly cannot
            silently keep the old spelling.
    """

    if not isinstance(text, str):
        raise LicenseRuleError(
            f"a license identifier must be a string, got {type(text).__name__}",
            code="license_invalid",
            subject=str(text),
        )
    collapsed = " ".join(text.split())
    if not collapsed:
        raise LicenseRuleError("a license identifier must not be empty", code="license_invalid")
    lowered = collapsed.lower()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    canonical = "".join(collapsed.split())
    if canonical in _DEPRECATED:
        raise LicenseRuleError(
            f"{canonical} is a deprecated SPDX identifier; use {_DEPRECATED[canonical]}",
            code="license_deprecated",
            subject=canonical,
        )
    return canonical


def _attempt(text: str) -> tuple[str | None, str | None]:
    """Normalize without raising. Returns ``(identifier, failure_code)``."""

    try:
        return normalize_spdx(text), None
    except LicenseRuleError as error:
        return None, error.code


def check_license(
    manifest: ReleaseManifest, allowed: Iterable[str] = ("Apache-2.0", "MIT", "BSD-3-Clause")
) -> tuple[LicenseFinding, ...]:
    """Every license rule the manifest violates, in a stable order.

    Order: the identifier itself, then its deprecation, then the allow list, then the bundled files.
    """

    findings: list[LicenseFinding] = []
    raw = manifest.license.spdx
    spelled = "".join(raw.split()) if isinstance(raw, str) else ""
    canonical: str | None = None

    if spelled in _DEPRECATED:
        # A deprecated identifier is readable and must not stop an audit: report the replacement.
        canonical = _DEPRECATED[spelled]
        findings.append(
            LicenseFinding(
                code="license_deprecated",
                subject="license.spdx",
                message=f"license.spdx {raw!r} is deprecated; use {canonical}",
                severity="warning",
            )
        )
    else:
        normalized, failure = _attempt(raw)
        if normalized is None:
            findings.append(
                LicenseFinding(
                    code=failure or "license_invalid",
                    subject="license.spdx",
                    message=f"license.spdx {raw!r} is not a usable SPDX identifier",
                )
            )
        canonical = normalized

    if canonical is not None:
        allow_list = {
            normalized
            for normalized in (_attempt(str(item))[0] for item in allowed)
            if normalized is not None
        }
        if canonical not in allow_list:
            findings.append(
                LicenseFinding(
                    code="license_not_allowed",
                    subject="license.spdx",
                    message=(
                        f"license.spdx {canonical!r} is not in the allowed set "
                        f"{sorted(allow_list)}"
                    ),
                )
            )

    files = manifest.license.files
    if not files and (canonical or raw).upper() != _NO_FILE_REQUIRED:
        findings.append(
            LicenseFinding(
                code="license_file_missing",
                subject="license.files",
                message="a published license must ship at least one license file",
            )
        )
    for index, entry in enumerate(files):
        problem = _relative_problem(entry)
        if problem is not None:
            findings.append(
                LicenseFinding(
                    code="license_file_path",
                    subject=f"license.files[{index}]",
                    message=f"license.files entry {entry!r} {problem}",
                )
            )
    return tuple(findings)


def _relative_problem(entry: str) -> str | None:
    """Why ``entry`` is not a plain relative path, or ``None``.

    Only the minimum this layer needs: an absolute path or a parent traversal cannot describe a file
    bundled inside the distribution. Symlink and containment safety belong to the artifact layer.
    """

    normalized = entry.replace("\\", "/").strip()
    if not normalized:
        return "is empty"
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        return "is absolute"
    if any(part == ".." for part in normalized.split("/")):
        return "escapes its directory"
    return None


__all__ = ["LicenseFinding", "check_license", "normalize_spdx"]
