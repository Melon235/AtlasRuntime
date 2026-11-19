"""Assembling and rendering an audit report.

:func:`releaseguard.audit.service.audit_release` collects checks; this module decides what a *set* of
checks means. Keeping the two apart is what makes the aggregate rules testable on their own: a caller
can hand :func:`build_report` any combination of checks and see exactly how it is summarised.

Nothing here reads a file, a clock or an environment variable.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..metadata.manifest_parser import ReleaseManifest
from .contracts import (
    CHECK_ORDER,
    DEFAULT_POLICY,
    AuditCheck,
    AuditIssue,
    AuditPolicy,
    AuditReport,
    CheckStatus,
    Severity,
)
from .errors import AuditContractError


def build_report(
    checks: Iterable[AuditCheck],
    *,
    manifest: ReleaseManifest | None = None,
    artifact_count: int = 0,
    policy: AuditPolicy | None = None,
) -> AuditReport:
    """Aggregate ``checks`` into one report.

    Contract:

    * ``checks`` are returned in :data:`~releaseguard.audit.contracts.CHECK_ORDER`, whatever order
      they arrive in. A duplicated ``check_id`` is a caller bug and raises
      :class:`~releaseguard.audit.errors.AuditContractError` with ``code="duplicate_check"``.
    * ``issues`` is the flattened, de-duplicated-free union of every check's issues, sorted by
      :meth:`AuditIssue.sort_key` — severity first, then code, then subject.
    * ``status`` is :attr:`CheckStatus.FAIL` when any issue is an error, or when the policy sets
      ``fail_on_warnings`` and any issue is a warning. Otherwise it is :attr:`CheckStatus.PASS`.
      A skipped check is not a failure by itself.
    * ``summary`` counts checks by status plus ``errors``, ``warnings`` and ``issues``.
    * ``manifest_name`` / ``manifest_version`` come from ``manifest`` when one was supplied.
    """

    raise NotImplementedError("the audit report is not assembled yet")


def render_report(report: AuditReport) -> str:
    """Render ``report`` as its stable JSON text.

    The text is ``report.as_dict()`` serialised with sorted keys, two-space indentation and a
    trailing newline, so two renders of the same report are byte-identical and diffable.
    """

    raise NotImplementedError("the audit report is not rendered yet")


__all__ = ["build_report", "render_report"]
