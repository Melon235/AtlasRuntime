"""Reference implementation of ``releaseguard/audit/service.py`` for the Stage S1 fixture.

Never provisioned into the sandbox. Written to be dropped in at ``releaseguard/audit/service.py``.
"""

from __future__ import annotations

from pathlib import Path

from ..artifacts.checksums import DigestFormatError, normalize_digest
from ..artifacts.errors import ArtifactError
from ..artifacts.path_rules import normalize_artifact_path
from ..artifacts.scanner import ArtifactInventory, scan_artifacts
from ..metadata.errors import ReleaseMetadataError
from ..metadata.license_rules import check_license
from ..metadata.manifest_parser import ReleaseManifest, parse_manifest
from ..metadata.version_rules import check_version
from .contracts import (
    CHECK_CHECKSUMS,
    CHECK_COVERAGE,
    CHECK_EXTRAS,
    CHECK_INVENTORY,
    CHECK_LICENSE,
    CHECK_MANIFEST,
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
from .report import build_report


def _issue(
    code: str, subject: str, message: str, severity: Severity = Severity.ERROR
) -> AuditIssue:
    return AuditIssue(code=code, severity=severity, subject=subject, message=message)


def _check(check_id: str, issues: list[AuditIssue], detail: str) -> AuditCheck:
    failed = any(issue.severity is Severity.ERROR for issue in issues)
    return AuditCheck(
        check_id=check_id,
        status=CheckStatus.FAIL if failed else CheckStatus.PASS,
        issues=tuple(issues),
        detail=detail,
    )


def _skip(check_id: str, reason: str) -> AuditCheck:
    return AuditCheck(check_id=check_id, status=CheckStatus.SKIP, detail=reason)


def audit_release(
    manifest_path: str | Path,
    artifact_root: str | Path,
    *,
    policy: AuditPolicy | None = None,
) -> AuditReport:
    effective = policy or DEFAULT_POLICY
    checks: list[AuditCheck] = []

    manifest: ReleaseManifest | None = None
    try:
        manifest = parse_manifest(manifest_path)
    except ReleaseMetadataError as error:
        checks.append(
            _check(
                CHECK_MANIFEST,
                [_issue(error.code, error.subject or "manifest", str(error))],
                "the manifest could not be read",
            )
        )
    else:
        checks.append(
            AuditCheck(
                check_id=CHECK_MANIFEST,
                status=CheckStatus.PASS,
                detail=f"{manifest.name} {manifest.version} from {manifest.source}",
            )
        )

    if manifest is None:
        checks.append(_skip(CHECK_VERSION, "the manifest could not be parsed"))
        checks.append(_skip(CHECK_LICENSE, "the manifest could not be parsed"))
    else:
        checks.append(
            _check(
                CHECK_VERSION,
                [
                    _issue(finding.code, finding.subject, finding.message, Severity(finding.severity))
                    for finding in check_version(manifest)
                ],
                f"channel {manifest.channel}",
            )
        )
        try:
            license_findings = [
                _issue(
                    finding.code, finding.subject, finding.message, Severity(finding.severity)
                )
                for finding in check_license(
                    manifest, allowed=effective.allowed_licenses
                )
            ]
            checks.append(
                _check(CHECK_LICENSE, license_findings, f"license {manifest.license.spdx}")
            )
        except ReleaseMetadataError as error:
            checks.append(
                _check(
                    CHECK_LICENSE,
                    [_issue(error.code, error.subject or "license.spdx", str(error))],
                    "the license could not be evaluated",
                )
            )

    inventory: ArtifactInventory | None = None
    try:
        inventory = scan_artifacts(artifact_root)
    except ArtifactError as error:
        checks.append(
            _check(
                CHECK_INVENTORY,
                [_issue(error.code, error.subject or str(artifact_root), str(error))],
                "the build directory could not be scanned",
            )
        )
    else:
        checks.append(
            AuditCheck(
                check_id=CHECK_INVENTORY,
                status=CheckStatus.PASS,
                detail=f"{len(inventory.entries)} artifacts, {len(inventory.skipped)} refused",
            )
        )

    if inventory is None:
        reason = "the build directory could not be scanned"
        for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):
            checks.append(_skip(check_id, reason))
    elif manifest is None:
        reason = "the manifest could not be parsed, so there is nothing to compare"
        for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):
            checks.append(_skip(check_id, reason))
    else:
        checks.extend(_artifact_checks(manifest, inventory, effective))

    return build_report(
        checks,
        manifest=manifest,
        artifact_count=len(inventory.entries) if inventory is not None else 0,
        policy=effective,
    )


def _artifact_checks(
    manifest: ReleaseManifest, inventory: ArtifactInventory, policy: AuditPolicy
) -> list[AuditCheck]:
    scanned = inventory.by_path()

    # -- 4. paths: every declaration, then every entry the scanner refused.
    path_issues: list[AuditIssue] = []
    usable: dict[str, str] = {}
    for entry in manifest.artifacts:
        try:
            usable[entry.path] = normalize_artifact_path(entry.path)
        except ArtifactError as error:
            path_issues.append(_issue(error.code, entry.path, str(error)))
    for refused in inventory.skipped:
        path_issues.append(_issue(refused.code, refused.path, refused.message))

    # -- 5. checksums: only for declarations that name something we can find.
    checksum_issues: list[AuditIssue] = []
    for entry in manifest.artifacts:
        canonical = usable.get(entry.path)
        if canonical is None:
            continue
        found = scanned.get(canonical)
        if found is None:
            continue
        try:
            declared = normalize_digest(entry.sha256)
        except DigestFormatError as error:
            checksum_issues.append(_issue(error.code, entry.path, str(error)))
            continue
        if declared != found.sha256:
            checksum_issues.append(
                _issue(
                    "digest_mismatch",
                    entry.path,
                    f"declared {declared} but the file hashes to {found.sha256}",
                )
            )
            continue
        if entry.size != found.size:
            checksum_issues.append(
                _issue(
                    "size_mismatch",
                    entry.path,
                    f"declared {entry.size} bytes but the file is {found.size} bytes",
                )
            )

    # -- 6. coverage: a declaration with a usable path that is not in the inventory.
    coverage_issues: list[AuditIssue] = [
        _issue(
            "artifact_missing",
            entry.path,
            f"{entry.path} is declared but {manifest.source} names no such file in the inventory",
        )
        for entry in manifest.artifacts
        if usable.get(entry.path) is not None and usable[entry.path] not in scanned
    ]

    # -- 7. extras: an inventory entry nobody declared.
    declared = set(usable.values())
    extras_severity = Severity.WARNING if policy.allow_extra_artifacts else Severity.ERROR
    extras_issues: list[AuditIssue] = [
        _issue(
            "undeclared_artifact",
            entry.path,
            f"{entry.path} is in the build directory but not declared in the manifest",
            extras_severity,
        )
        for entry in inventory.entries
        if entry.path not in declared
    ]

    return [
        _check(
            CHECK_PATHS,
            path_issues,
            f"{len(manifest.artifacts)} declared paths, {len(inventory.skipped)} refused entries",
        ),
        _check(
            CHECK_CHECKSUMS,
            checksum_issues,
            f"{sum(1 for entry in manifest.artifacts if usable.get(entry.path) in scanned)} compared",
        ),
        _check(CHECK_COVERAGE, coverage_issues, f"{len(manifest.artifacts)} declared artifacts"),
        _check(CHECK_EXTRAS, extras_issues, f"{len(inventory.entries)} inventoried files"),
    ]
