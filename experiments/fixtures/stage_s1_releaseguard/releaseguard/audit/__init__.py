"""The shared release readiness audit.

``audit_release`` is the entry point; ``build_report`` and ``render_report`` are the assembly and
rendering halves. The contracts module is the report shape consumers parse.
"""

from __future__ import annotations

from .contracts import (
    CHECK_CHECKSUMS,
    CHECK_COVERAGE,
    CHECK_EXTRAS,
    CHECK_INVENTORY,
    CHECK_LICENSE,
    CHECK_MANIFEST,
    CHECK_ORDER,
    CHECK_PATHS,
    CHECK_VERSION,
    DEFAULT_POLICY,
    AuditCheck,
    AuditIssue,
    AuditPolicy,
    AuditReport,
    CheckStatus,
    Severity,
)
from .errors import AuditContractError, AuditError, AuditNotImplementedError
from .report import build_report, render_report
from .service import audit_release

__all__ = [
    "CHECK_CHECKSUMS",
    "CHECK_COVERAGE",
    "CHECK_EXTRAS",
    "CHECK_INVENTORY",
    "CHECK_LICENSE",
    "CHECK_MANIFEST",
    "CHECK_ORDER",
    "CHECK_PATHS",
    "CHECK_VERSION",
    "DEFAULT_POLICY",
    "AuditCheck",
    "AuditContractError",
    "AuditError",
    "AuditIssue",
    "AuditNotImplementedError",
    "AuditPolicy",
    "AuditReport",
    "CheckStatus",
    "Severity",
    "audit_release",
    "build_report",
    "render_report",
]
