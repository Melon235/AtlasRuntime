"""Reference implementation of ``releaseguard/audit/report.py`` for the Stage S1 fixture.

This file never enters the sandbox. The offline verifier copies it over the stub in a scratch copy of
the fixture and uses the result to prove that the frozen fixture is solvable.

It is written to be dropped in at ``releaseguard/audit/report.py``, so it uses that module's
relative imports.
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
    effective = policy or DEFAULT_POLICY
    ordered: dict[str, AuditCheck] = {}
    for check in checks:
        if check.check_id in ordered:
            raise AuditContractError(
                f"check {check.check_id!r} was supplied twice", code="duplicate_check"
            )
        ordered[check.check_id] = check

    sequence = tuple(ordered[check_id] for check_id in CHECK_ORDER if check_id in ordered)
    issues = tuple(
        sorted(
            (issue for check in sequence for issue in check.issues),
            key=AuditIssue.sort_key,
        )
    )
    errors = sum(1 for issue in issues if issue.severity is Severity.ERROR)
    warnings = sum(1 for issue in issues if issue.severity is Severity.WARNING)
    summary = {
        "checks": len(sequence),
        "passed": sum(1 for check in sequence if check.status is CheckStatus.PASS),
        "failed": sum(1 for check in sequence if check.status is CheckStatus.FAIL),
        "skipped": sum(1 for check in sequence if check.status is CheckStatus.SKIP),
        "errors": errors,
        "warnings": warnings,
        "issues": len(issues),
    }
    failed = bool(errors) or (effective.fail_on_warnings and bool(warnings))
    return AuditReport(
        status=CheckStatus.FAIL if failed else CheckStatus.PASS,
        checks=sequence,
        issues=issues,
        summary=summary,
        manifest_name=manifest.name if manifest is not None else None,
        manifest_version=manifest.version if manifest is not None else None,
        artifact_count=artifact_count,
        policy=effective,
    )


def render_report(report: AuditReport) -> str:
    return json.dumps(report.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
