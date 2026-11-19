"""The metadata failure vocabulary.

Every failure a caller can act on is one of these exceptions and carries a machine-readable
``code`` plus the ``subject`` that failed (a dotted manifest key, or a manifest path). Callers are
expected to branch on ``code`` and never on the message text: the message is documentation for a
human reading a report.

``severity`` is not carried here. A metadata failure is an error or a warning depending on the rule,
and that decision belongs to the rule that found it — see :class:`VersionFinding` and
:class:`LicenseFinding`.
"""

from __future__ import annotations

from typing import Literal

#: Severity of a rule finding. These literals are the audit contract's severity vocabulary; the
#: metadata package repeats them rather than importing the audit package, because metadata must stay
#: usable without the shared layer.
Severity = Literal["error", "warning", "info"]


class ReleaseMetadataError(ValueError):
    """Base class for metadata failures."""

    default_code = "metadata_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        subject: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.subject = subject

    def as_dict(self) -> dict[str, str | None]:
        """The failure as a report-shaped mapping."""

        return {"code": self.code, "subject": self.subject, "message": str(self)}


class ManifestFormatError(ReleaseMetadataError):
    """The manifest is not the document this project reads at all."""

    default_code = "manifest_format"


class ManifestFieldError(ReleaseMetadataError):
    """The manifest is a mapping, but a required field is missing or has the wrong type."""

    default_code = "manifest_field"


class VersionRuleError(ReleaseMetadataError):
    """A version string cannot be parsed as a release version."""

    default_code = "version_invalid"


class LicenseRuleError(ReleaseMetadataError):
    """A license identifier cannot be normalized to an SPDX identifier."""

    default_code = "license_invalid"


__all__ = [
    "LicenseRuleError",
    "ManifestFieldError",
    "ManifestFormatError",
    "ReleaseMetadataError",
    "Severity",
    "VersionRuleError",
]
