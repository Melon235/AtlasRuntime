"""The artifact failure vocabulary.

Everything in this package fails by raising one of these. That is a different convention from the
metadata package on purpose: a manifest carries several independent rules and is worth reporting in
full, while a path or a digest is either usable or it is not, and there is exactly one thing to say
about it.

Each error carries a machine-readable ``code`` and the ``subject`` it is about, so the audit layer can
put it in a report without parsing a message.
"""

from __future__ import annotations


class ArtifactError(Exception):
    """Base class for artifact failures."""

    default_code = "artifact_error"

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
        return {"code": self.code, "subject": self.subject, "message": str(self)}


class ArtifactPathError(ArtifactError):
    """A string cannot be used as an artifact path inside a build directory."""

    default_code = "path_invalid"


class DigestFormatError(ArtifactError):
    """A digest is not a SHA-256 digest."""

    default_code = "digest_format"


class ArtifactScanError(ArtifactError):
    """A build directory cannot be scanned."""

    default_code = "artifact_scan"


__all__ = [
    "ArtifactError",
    "ArtifactPathError",
    "ArtifactScanError",
    "DigestFormatError",
]
