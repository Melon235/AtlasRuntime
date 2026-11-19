"""The audit report vocabulary.

This module is the published shape of an audit. Consumers read it, diff it and store it, so it is
deliberately boring: strings, integers and lists of strings, in a fixed order, with no timestamps, no
absolute paths and no map iteration order leaking into the output.

Three rules about ordering matter, because a report that reorders itself is not machine-readable:

* ``checks`` are reported in :data:`CHECK_ORDER`, which is the audit's logical order (metadata,
  then artifacts, then integration). It is **not** alphabetical.
* ``issues`` are sorted by severity (error, warning, info), then ``code``, then ``subject``.
* every mapping in :meth:`AuditReport.as_dict` is constructed in a fixed key order.

The check ids below are a closed vocabulary. An audit fills them in; it does not invent new ones, and
a consumer may rely on all eight being present.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# -- check ids -----------------------------------------------------------------------------------

CHECK_MANIFEST = "metadata.manifest"
CHECK_VERSION = "metadata.version"
CHECK_LICENSE = "metadata.license"
CHECK_INVENTORY = "artifacts.inventory"
CHECK_PATHS = "artifacts.paths"
CHECK_CHECKSUMS = "artifacts.checksums"
CHECK_COVERAGE = "integration.coverage"
CHECK_EXTRAS = "integration.extras"

#: The order a report lists its checks in. Logical, not alphabetical.
CHECK_ORDER: tuple[str, ...] = (
    CHECK_MANIFEST,
    CHECK_VERSION,
    CHECK_LICENSE,
    CHECK_INVENTORY,
    CHECK_PATHS,
    CHECK_CHECKSUMS,
    CHECK_COVERAGE,
    CHECK_EXTRAS,
)

#: Set form of :data:`CHECK_ORDER`, used to validate a check id without a linear scan.
CONTROL_ID_SET = frozenset(CHECK_ORDER)

CATEGORY_METADATA = "metadata"
CATEGORY_ARTIFACTS = "artifacts"
CATEGORY_INTEGRATION = "integration"

#: Which layer each check belongs to. A consumer groups by this without parsing the id.
CATEGORY_BY_CHECK: Mapping[str, str] = {
    CHECK_MANIFEST: CATEGORY_METADATA,
    CHECK_VERSION: CATEGORY_METADATA,
    CHECK_LICENSE: CATEGORY_METADATA,
    CHECK_INVENTORY: CATEGORY_ARTIFACTS,
    CHECK_PATHS: CATEGORY_ARTIFACTS,
    CHECK_CHECKSUMS: CATEGORY_ARTIFACTS,
    CHECK_COVERAGE: CATEGORY_INTEGRATION,
    CHECK_EXTRAS: CATEGORY_INTEGRATION,
}


class CheckStatus(StrEnum):
    """Terminal state of one check."""

    PASS = "pass"
    FAIL = "fail"
    #: The check could not be evaluated because an earlier, blocking check failed. Not a pass.
    SKIP = "skip"


class Severity(StrEnum):
    """How much one issue matters.

    ``ERROR`` makes the release unready. ``WARNING`` never does on its own — unless the policy asks
    for it (:attr:`AuditPolicy.fail_on_warnings`).
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


#: Sort key for issues: errors first.
SEVERITY_ORDER: Mapping[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


# -- policy --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditPolicy:
    """What "ready" means for this release. Host-owned; the model never supplies it at run time."""

    allowed_licenses: tuple[str, ...] = ("Apache-2.0", "MIT", "BSD-3-Clause")
    #: When ``True`` an undeclared file in the build directory is a warning. When ``False`` it is an
    #: error: the release would ship content nobody reviewed.
    allow_extra_artifacts: bool = True
    #: Promote warnings to a failing report. Used by gates that refuse to publish anything imperfect.
    fail_on_warnings: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allowed_licenses, tuple):
            raise TypeError("allowed_licenses must be a tuple of strings")

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_licenses": list(self.allowed_licenses),
            "allow_extra_artifacts": self.allow_extra_artifacts,
            "fail_on_warnings": self.fail_on_warnings,
        }


DEFAULT_POLICY = AuditPolicy()


# -- findings ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditIssue:
    """One concrete problem, at file or metadata level."""

    code: str
    severity: Severity
    subject: str
    message: str

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("an audit issue requires a non-empty code")
        if not isinstance(self.severity, Severity):
            raise TypeError("severity must be a Severity")

    def sort_key(self) -> tuple[int, str, str, str]:
        return (SEVERITY_ORDER[self.severity], self.code, self.subject, self.message)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "subject": self.subject,
            "message": self.message,
        }


@dataclass(frozen=True)
class AuditCheck:
    """The outcome of one check id.

    ``status`` is :attr:`CheckStatus.FAIL` when the check found at least one error-level issue,
    :attr:`CheckStatus.SKIP` when it could not be evaluated at all, and :attr:`CheckStatus.PASS`
    otherwise — warnings alone do not fail a check, they travel in ``issues``.
    """

    check_id: str
    status: CheckStatus
    issues: tuple[AuditIssue, ...] = ()
    #: Free-form evidence: counts, the reason a check was skipped. Never parsed by a consumer.
    detail: str = ""

    def __post_init__(self) -> None:
        if self.check_id not in CONTROL_ID_SET:
            raise ValueError(f"{self.check_id!r} is not an audit check id")
        if not isinstance(self.status, CheckStatus):
            raise TypeError("status must be a CheckStatus")

    @property
    def category(self) -> str:
        return CATEGORY_BY_CHECK[self.check_id]

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "category": self.category,
            "status": str(self.status),
            "detail": self.detail,
            "issues": [issue.as_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class AuditReport:
    """The whole audit: one status, every check, every issue, and a summary of the counts."""

    status: CheckStatus
    checks: tuple[AuditCheck, ...] = field(default=())
    issues: tuple[AuditIssue, ...] = field(default=())
    summary: Mapping[str, int] = field(default_factory=dict)
    manifest_name: str | None = None
    manifest_version: str | None = None
    artifact_count: int = 0
    policy: AuditPolicy = DEFAULT_POLICY

    def as_dict(self) -> dict[str, Any]:
        """The stable, machine-readable form. Key order is part of the shape."""

        return {
            "status": str(self.status),
            "manifest": (
                None
                if self.manifest_name is None
                else {"name": self.manifest_name, "version": self.manifest_version}
            ),
            "artifact_count": self.artifact_count,
            "policy": self.policy.as_dict(),
            "summary": {
                "checks": int(self.summary.get("checks", 0)),
                "passed": int(self.summary.get("passed", 0)),
                "failed": int(self.summary.get("failed", 0)),
                "skipped": int(self.summary.get("skipped", 0)),
                "errors": int(self.summary.get("errors", 0)),
                "warnings": int(self.summary.get("warnings", 0)),
                "issues": int(self.summary.get("issues", 0)),
            },
            "checks": [check.as_dict() for check in self.checks],
            "issues": [issue.as_dict() for issue in self.issues],
        }


__all__ = [
    "CATEGORY_ARTIFACTS",
    "CATEGORY_BY_CHECK",
    "CATEGORY_INTEGRATION",
    "CATEGORY_METADATA",
    "CHECK_CHECKSUMS",
    "CHECK_COVERAGE",
    "CHECK_EXTRAS",
    "CHECK_INVENTORY",
    "CHECK_LICENSE",
    "CHECK_MANIFEST",
    "CHECK_ORDER",
    "CHECK_PATHS",
    "CHECK_VERSION",
    "CONTROL_ID_SET",
    "DEFAULT_POLICY",
    "SEVERITY_ORDER",
    "AuditCheck",
    "AuditIssue",
    "AuditPolicy",
    "AuditReport",
    "CheckStatus",
    "Severity",
]
