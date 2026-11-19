"""The shared release readiness audit.

This is the acceptance suite for the audit layer. It fails today: ``audit.service`` and
``audit.report`` are incomplete.

Consumers depend on three things and each is asserted here: a report says ready or not ready, every
check appears exactly once in the documented order, and the rendered text is the same every time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from releaseguard.audit import (
    CHECK_CHECKSUMS,
    CHECK_COVERAGE,
    CHECK_EXTRAS,
    CHECK_INVENTORY,
    CHECK_LICENSE,
    CHECK_MANIFEST,
    CHECK_ORDER,
    CHECK_PATHS,
    CHECK_VERSION,
    AuditContractError,
    AuditIssue,
    AuditPolicy,
    AuditReport,
    CheckStatus,
    Severity,
    audit_release,
    build_report,
    render_report,
)
from releaseguard.audit.contracts import AuditCheck

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
DIST = SAMPLES / "dist"
MANIFEST = SAMPLES / "release-manifest.json"


# --------------------------------------------------------------------------- helpers


def _payload() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _statuses(report: AuditReport) -> dict[str, CheckStatus]:
    return {check.check_id: check.status for check in report.checks}


def _issues(report: AuditReport, check_id: str) -> tuple[AuditIssue, ...]:
    return next(check for check in report.checks if check.check_id == check_id).issues


# --------------------------------------------------------------------------- the healthy release


def test_a_healthy_release_is_ready() -> None:
    report = audit_release(MANIFEST, DIST)

    assert report.status is CheckStatus.PASS
    assert [check.check_id for check in report.checks] == list(CHECK_ORDER)
    # Three declared artifacts plus the undeclared packager log the inventory still accepted.
    assert report.artifact_count == 4
    assert report.manifest_name == "releaseguard"
    assert report.manifest_version == "2.4.0"
    assert _statuses(report) == {check_id: CheckStatus.PASS for check_id in CHECK_ORDER}
    assert report.summary["checks"] == 8
    assert report.summary["passed"] == 8
    assert report.summary["failed"] == 0
    assert report.summary["skipped"] == 0
    assert report.summary["errors"] == 0
    # The build directory contains a packager log nobody declared: tolerated, but reported.
    assert report.summary["warnings"] == 1
    assert report.summary["issues"] == 1


def test_the_undeclared_file_is_a_warning_on_the_extras_check() -> None:
    report = audit_release(MANIFEST, DIST)

    extras = _issues(report, CHECK_EXTRAS)
    assert [issue.code for issue in extras] == ["undeclared_artifact"]
    assert extras[0].subject == "extra-debug.log"
    assert extras[0].severity is Severity.WARNING
    assert _issues(report, CHECK_COVERAGE) == ()
    assert _issues(report, CHECK_CHECKSUMS) == ()


def test_the_report_dict_has_the_documented_shape() -> None:
    payload = audit_release(MANIFEST, DIST).as_dict()

    assert list(payload) == [
        "status",
        "manifest",
        "artifact_count",
        "policy",
        "summary",
        "checks",
        "issues",
    ]
    assert payload["status"] == "pass"
    assert payload["manifest"] == {"name": "releaseguard", "version": "2.4.0"}
    assert isinstance(payload["artifact_count"], int)
    assert payload["policy"]["allowed_licenses"] == ["Apache-2.0", "MIT", "BSD-3-Clause"]
    assert list(payload["summary"]) == [
        "checks",
        "passed",
        "failed",
        "skipped",
        "errors",
        "warnings",
        "issues",
    ]
    assert [check["check_id"] for check in payload["checks"]] == list(CHECK_ORDER)
    assert payload["checks"][0]["category"] == "metadata"
    assert payload["checks"][5]["category"] == "artifacts"
    assert payload["checks"][7]["category"] == "integration"


def test_the_rendered_report_is_identical_every_time() -> None:
    first = render_report(audit_release(MANIFEST, DIST))
    second = render_report(audit_release(MANIFEST, DIST))

    assert first == second
    assert first.endswith("\n")
    assert json.loads(first)["status"] == "pass"


def test_the_audit_does_not_modify_its_inputs(tmp_path: Path) -> None:
    before = {
        path: path.read_bytes() for path in sorted(DIST.iterdir()) if path.is_file()
    }
    manifest_before = MANIFEST.read_bytes()

    audit_release(MANIFEST, DIST)

    after = {path: path.read_bytes() for path in sorted(DIST.iterdir()) if path.is_file()}
    assert after == before
    assert MANIFEST.read_bytes() == manifest_before


# --------------------------------------------------------------------------- metadata findings


def test_a_broken_manifest_skips_exactly_what_it_makes_unevaluable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    report = audit_release(broken, DIST)

    assert report.status is CheckStatus.FAIL
    assert _statuses(report) == {
        CHECK_MANIFEST: CheckStatus.FAIL,
        CHECK_VERSION: CheckStatus.SKIP,
        CHECK_LICENSE: CheckStatus.SKIP,
        # The build directory is still worth looking at.
        CHECK_INVENTORY: CheckStatus.PASS,
        CHECK_PATHS: CheckStatus.SKIP,
        CHECK_CHECKSUMS: CheckStatus.SKIP,
        CHECK_COVERAGE: CheckStatus.SKIP,
        CHECK_EXTRAS: CheckStatus.SKIP,
    }
    assert report.artifact_count == 4
    assert report.manifest_name is None
    manifest_issues = _issues(report, CHECK_MANIFEST)
    assert [issue.code for issue in manifest_issues] == ["manifest_not_json"]
    assert manifest_issues[0].severity is Severity.ERROR


def test_a_version_rule_violation_fails_the_version_check_only(tmp_path: Path) -> None:
    payload = _payload()
    payload["release"]["version"] = "2.4.0rc1"
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert report.status is CheckStatus.FAIL
    assert _statuses(report)[CHECK_VERSION] is CheckStatus.FAIL
    assert _statuses(report)[CHECK_MANIFEST] is CheckStatus.PASS
    assert _statuses(report)[CHECK_LICENSE] is CheckStatus.PASS
    assert [issue.code for issue in _issues(report, CHECK_VERSION)] == ["prerelease_on_stable"]
    assert _issues(report, CHECK_VERSION)[0].subject == "release.version"


def test_a_license_violation_fails_the_license_check_only(tmp_path: Path) -> None:
    payload = _payload()
    payload["license"]["spdx"] = "GPL-3.0"
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert _statuses(report)[CHECK_LICENSE] is CheckStatus.FAIL
    codes = [issue.code for issue in _issues(report, CHECK_LICENSE)]
    assert codes == ["license_deprecated", "license_not_allowed"]
    severities = [issue.severity for issue in _issues(report, CHECK_LICENSE)]
    assert severities == [Severity.WARNING, Severity.ERROR]
    assert report.status is CheckStatus.FAIL


def test_the_policy_decides_which_licenses_are_allowed(tmp_path: Path) -> None:
    payload = _payload()
    payload["license"]["spdx"] = "Apache-2.0"
    manifest = _write_manifest(tmp_path, payload)

    permissive = audit_release(manifest, DIST, policy=AuditPolicy(allowed_licenses=("Apache-2.0",)))
    restrictive = audit_release(manifest, DIST, policy=AuditPolicy(allowed_licenses=("MIT",)))

    assert _statuses(permissive)[CHECK_LICENSE] is CheckStatus.PASS
    assert _statuses(restrictive)[CHECK_LICENSE] is CheckStatus.FAIL
    assert [issue.code for issue in _issues(restrictive, CHECK_LICENSE)] == ["license_not_allowed"]


# --------------------------------------------------------------------------- artifact findings


def test_a_missing_artifact_fails_coverage_and_not_checksums(tmp_path: Path) -> None:
    payload = _payload()
    payload["artifacts"].append(
        {"path": "releaseguard-2.4.0-docs.zip", "sha256": "d" * 64, "size": 12}
    )
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert _statuses(report)[CHECK_COVERAGE] is CheckStatus.FAIL
    assert _statuses(report)[CHECK_CHECKSUMS] is CheckStatus.PASS
    issues = _issues(report, CHECK_COVERAGE)
    assert [issue.code for issue in issues] == ["artifact_missing"]
    assert issues[0].subject == "releaseguard-2.4.0-docs.zip"


def test_a_digest_mismatch_names_the_artifact(tmp_path: Path) -> None:
    payload = _payload()
    payload["artifacts"][2]["sha256"] = "e" * 64
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert _statuses(report)[CHECK_CHECKSUMS] is CheckStatus.FAIL
    issues = _issues(report, CHECK_CHECKSUMS)
    assert [issue.code for issue in issues] == ["digest_mismatch"]
    assert issues[0].subject == "CHANGELOG.md"


def test_a_size_mismatch_is_a_different_issue_from_a_digest_mismatch(tmp_path: Path) -> None:
    payload = _payload()
    payload["artifacts"][2]["size"] = 1
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert [issue.code for issue in _issues(report, CHECK_CHECKSUMS)] == ["size_mismatch"]


def test_a_malformed_declared_digest_is_reported_rather_than_raised(tmp_path: Path) -> None:
    payload = _payload()
    payload["artifacts"][0]["sha256"] = "not-a-digest"
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert [issue.code for issue in _issues(report, CHECK_CHECKSUMS)] == ["digest_format"]
    assert _issues(report, CHECK_CHECKSUMS)[0].subject == "releaseguard-2.4.0-py3-none-any.whl"


def test_an_unsafe_declared_path_is_one_error_not_two(tmp_path: Path) -> None:
    payload = _payload()
    payload["artifacts"].append({"path": "../escape.whl", "sha256": "f" * 64, "size": 3})
    report = audit_release(_write_manifest(tmp_path, payload), DIST)

    assert _statuses(report)[CHECK_PATHS] is CheckStatus.FAIL
    assert _statuses(report)[CHECK_COVERAGE] is CheckStatus.PASS
    issues = _issues(report, CHECK_PATHS)
    assert [issue.code for issue in issues] == ["path_escape"]
    assert issues[0].subject == "../escape.whl"


def test_a_missing_build_directory_fails_the_inventory_and_skips_the_rest(tmp_path: Path) -> None:
    report = audit_release(MANIFEST, tmp_path / "absent")

    assert _statuses(report)[CHECK_INVENTORY] is CheckStatus.FAIL
    assert [issue.code for issue in _issues(report, CHECK_INVENTORY)] == ["artifact_root_missing"]
    for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):
        assert _statuses(report)[check_id] is CheckStatus.SKIP
    assert report.artifact_count == 0


def test_an_extra_artifact_can_be_made_fatal(tmp_path: Path) -> None:
    policy = AuditPolicy(allow_extra_artifacts=False)
    report = audit_release(MANIFEST, DIST, policy=policy)

    assert _statuses(report)[CHECK_EXTRAS] is CheckStatus.FAIL
    assert report.status is CheckStatus.FAIL
    assert [issue.severity for issue in _issues(report, CHECK_EXTRAS)] == [Severity.ERROR]


def test_warnings_can_be_promoted_by_policy() -> None:
    report = audit_release(MANIFEST, DIST, policy=AuditPolicy(fail_on_warnings=True))

    assert _statuses(report)[CHECK_EXTRAS] is CheckStatus.PASS
    assert report.status is CheckStatus.FAIL
    assert report.summary["errors"] == 0
    assert report.summary["warnings"] == 1


# --------------------------------------------------------------------------- report assembly


def _check(check_id: str, issue_severity: Severity | None, code: str = "x") -> AuditCheck:
    issues = (
        ()
        if issue_severity is None
        else (AuditIssue(code=code, severity=issue_severity, subject="s", message="m"),)
    )
    status = CheckStatus.FAIL if issue_severity is Severity.ERROR else CheckStatus.PASS
    return AuditCheck(check_id=check_id, status=status, issues=issues)


def test_build_report_returns_the_checks_in_the_documented_order() -> None:
    supplied = [
        _check(CHECK_EXTRAS, None),
        _check(CHECK_MANIFEST, None),
        _check(CHECK_CHECKSUMS, None),
    ]

    report = build_report(supplied)

    assert [check.check_id for check in report.checks] == [
        CHECK_MANIFEST,
        CHECK_CHECKSUMS,
        CHECK_EXTRAS,
    ]
    assert report.status is CheckStatus.PASS
    assert report.summary["checks"] == 3


def test_build_report_sorts_issues_by_severity_then_code() -> None:
    supplied = [
        _check(CHECK_LICENSE, Severity.WARNING, code="license_deprecated"),
        _check(CHECK_VERSION, Severity.ERROR, code="prerelease_on_stable"),
        _check(CHECK_EXTRAS, Severity.WARNING, code="artifact_extra"),
        _check(CHECK_MANIFEST, Severity.INFO, code="note"),
    ]

    report = build_report(supplied)

    assert [(issue.severity.value, issue.code) for issue in report.issues] == [
        ("error", "prerelease_on_stable"),
        ("warning", "artifact_extra"),
        ("warning", "license_deprecated"),
        ("info", "note"),
    ]
    assert report.summary == {
        "checks": 4,
        "passed": 3,
        "failed": 1,
        "skipped": 0,
        "errors": 1,
        "warnings": 2,
        "issues": 4,
    }


def test_build_report_refuses_a_duplicated_check() -> None:
    with pytest.raises(AuditContractError) as raised:
        build_report([_check(CHECK_MANIFEST, None), _check(CHECK_MANIFEST, None)])
    assert raised.value.code == "duplicate_check"


def test_build_report_counts_a_skipped_check_without_failing() -> None:
    supplied = [
        AuditCheck(check_id=CHECK_MANIFEST, status=CheckStatus.SKIP, detail="not attempted"),
    ]

    report = build_report(supplied)

    assert report.status is CheckStatus.PASS
    assert report.summary["skipped"] == 1
    assert report.checks[0].detail == "not attempted"
