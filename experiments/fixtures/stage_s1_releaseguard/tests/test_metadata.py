"""The metadata layer: manifest parsing, version rules, license rules.

These tests describe behaviour other tools already depend on. They pass today and must keep passing:
the summary report of any release depends on these rules being the ones that ran.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from releaseguard.metadata import (
    LicenseRuleError,
    ManifestFieldError,
    ManifestFormatError,
    VersionRuleError,
    check_license,
    check_version,
    compare_versions,
    normalize_spdx,
    parse_manifest,
    parse_manifest_text,
    parse_version,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _manifest(**overrides: object):
    payload: dict[str, object] = {
        "release": {"name": "demo", "version": "1.2.3"},
        "license": {"spdx": "MIT", "files": ["LICENSE"]},
        "artifacts": [{"path": "demo-1.2.3.tar.gz", "sha256": "a" * 64, "size": 10}],
    }
    for key, value in overrides.items():
        if key in ("release", "license") and isinstance(value, dict):
            payload[key] = {**payload[key], **value}  # type: ignore[dict-item]
        else:
            payload[key] = value
    return parse_manifest_text(json.dumps(payload))


# --------------------------------------------------------------------------- parsing the sample


def test_the_sample_manifest_parses_with_its_declared_artifacts() -> None:
    manifest = parse_manifest(SAMPLES / "release-manifest.json")

    assert manifest.name == "releaseguard"
    assert manifest.version == "2.4.0"
    assert manifest.channel == "stable"
    assert manifest.python_requirement == ">=3.9"
    assert manifest.license.spdx == "Apache-2.0"
    assert manifest.license.files == ("samples/LICENSE",)
    assert manifest.artifact_paths() == (
        "releaseguard-2.4.0-py3-none-any.whl",
        "releaseguard-2.4.0.tar.gz",
        "CHANGELOG.md",
    )
    assert [entry.kind for entry in manifest.artifacts] == ["wheel", "sdist", "document"]
    assert all(len(entry.sha256) == 64 for entry in manifest.artifacts)


def test_defaults_apply_to_an_omitted_field() -> None:
    manifest = parse_manifest_text(
        json.dumps(
            {
                "release": {"name": "demo", "version": "1.2.3"},
                "license": {"spdx": "MIT"},
                "artifacts": [{"path": "demo.tar.gz", "sha256": "b" * 64, "size": 1}],
            }
        )
    )

    assert manifest.channel == "stable"
    assert manifest.python_requirement == ">=3.9"
    assert manifest.license.files == ()
    assert manifest.artifacts[0].kind == "binary"
    assert manifest.artifacts[0].size == 1


def test_unknown_top_level_keys_are_ignored() -> None:
    manifest = parse_manifest_text(
        json.dumps(
            {
                "release": {"name": "demo", "version": "1.2.3", "notes": "ignored"},
                "license": {"spdx": "MIT"},
                "artifacts": [],
                "provenance": {"builder": "ci"},
            }
        )
    )

    assert manifest.name == "demo"
    assert manifest.artifacts == ()


# --------------------------------------------------------------------------- parse failures


def test_a_document_that_is_not_json_is_refused() -> None:
    with pytest.raises(ManifestFormatError) as raised:
        parse_manifest_text("release: demo", source="inline")
    assert raised.value.code == "manifest_not_json"
    assert raised.value.subject == "inline"


def test_a_root_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(ManifestFormatError) as raised:
        parse_manifest_text("[]")
    assert raised.value.code == "manifest_not_object"


def test_a_missing_section_names_the_section() -> None:
    with pytest.raises(ManifestFieldError) as raised:
        parse_manifest_text(json.dumps({"release": {"name": "demo", "version": "1.0.0"}}))
    assert raised.value.code == "field_missing"
    assert raised.value.subject == "license"


def test_a_missing_release_field_names_the_dotted_key() -> None:
    with pytest.raises(ManifestFieldError) as raised:
        parse_manifest_text(
            json.dumps(
                {
                    "release": {"name": "demo"},
                    "license": {"spdx": "MIT"},
                    "artifacts": [],
                }
            )
        )
    assert raised.value.code == "field_missing"
    assert raised.value.subject == "release.version"


def test_a_mistyped_size_is_refused_before_it_reaches_the_comparison() -> None:
    with pytest.raises(ManifestFieldError) as raised:
        parse_manifest_text(
            json.dumps(
                {
                    "release": {"name": "demo", "version": "1.0.0"},
                    "license": {"spdx": "MIT"},
                    "artifacts": [{"path": "a.tar.gz", "sha256": "c" * 64, "size": "10"}],
                }
            )
        )
    assert raised.value.code == "field_type"
    assert raised.value.subject == "artifacts[0].size"


def test_a_negative_size_is_refused() -> None:
    with pytest.raises(ManifestFieldError) as raised:
        parse_manifest_text(
            json.dumps(
                {
                    "release": {"name": "demo", "version": "1.0.0"},
                    "license": {"spdx": "MIT"},
                    "artifacts": [{"path": "a.tar.gz", "sha256": "c" * 64, "size": -1}],
                }
            )
        )
    assert raised.value.code == "field_value"


def test_a_duplicate_artifact_path_is_refused() -> None:
    entry = {"path": "a.tar.gz", "sha256": "c" * 64, "size": 3}
    with pytest.raises(ManifestFieldError) as raised:
        parse_manifest_text(
            json.dumps(
                {
                    "release": {"name": "demo", "version": "1.0.0"},
                    "license": {"spdx": "MIT"},
                    "artifacts": [entry, dict(entry)],
                }
            )
        )
    assert raised.value.code == "duplicate_artifact"
    assert raised.value.subject == "a.tar.gz"


def test_an_unreadable_manifest_is_a_format_failure(tmp_path: Path) -> None:
    with pytest.raises(ManifestFormatError) as raised:
        parse_manifest(tmp_path / "absent.json")
    assert raised.value.code == "manifest_unreadable"


# --------------------------------------------------------------------------- versions


def test_versions_parse_and_compare() -> None:
    assert str(parse_version("2.4.0")) == "2.4.0"
    assert str(parse_version("2.4.0rc1")) == "2.4.0rc1"
    assert parse_version("2.5.0b2+build.7").local == "build.7"

    assert compare_versions("2.4.0rc1", "2.4.0") < 0
    assert compare_versions("2.4.0a2", "2.4.0b1") < 0
    assert compare_versions("2.4.0", "2.4.0") == 0
    assert compare_versions("2.5.0", "2.4.9") > 0


@pytest.mark.parametrize("text", ["2.4", "2.4.0.1", "2.4.0-rc1", "v2.4.0", "", "02.4.0"])
def test_a_string_that_is_not_a_version_is_refused(text: str) -> None:
    with pytest.raises(VersionRuleError) as raised:
        parse_version(text)
    assert raised.value.code == "version_invalid"


def test_a_prerelease_on_the_stable_channel_is_an_error() -> None:
    findings = check_version(_manifest(release={"version": "1.2.3rc1"}))
    assert [finding.code for finding in findings] == ["prerelease_on_stable"]
    assert findings[0].subject == "release.version"
    assert findings[0].severity == "error"


def test_a_final_version_on_the_candidate_channel_is_an_error() -> None:
    findings = check_version(_manifest(release={"channel": "candidate", "version": "1.2.3"}))
    assert [finding.code for finding in findings] == ["candidate_requires_prerelease"]


def test_the_nightly_channel_imposes_no_version_shape() -> None:
    assert check_version(_manifest(release={"channel": "nightly", "version": "1.2.3"})) == ()
    assert check_version(_manifest(release={"channel": "nightly", "version": "1.2.3rc1"})) == ()


def test_an_unknown_channel_is_an_error() -> None:
    findings = check_version(_manifest(release={"channel": "beta"}))
    assert [finding.code for finding in findings] == ["unknown_channel"]
    assert findings[0].subject == "release.channel"


def test_an_unparseable_version_is_reported_not_raised() -> None:
    findings = check_version(_manifest(release={"version": "1.2"}))
    assert [finding.code for finding in findings] == ["version_invalid"]


def test_a_malformed_python_requirement_is_an_error() -> None:
    findings = check_version(_manifest(release={"python_requirement": "3.9"}))
    assert [finding.code for finding in findings] == ["python_requirement_invalid"]
    assert findings[0].subject == "release.python_requirement"


def test_a_python_requirement_below_the_floor_is_an_error() -> None:
    findings = check_version(_manifest(release={"python_requirement": ">=3.8"}))
    assert [finding.code for finding in findings] == ["python_requirement_unsupported"]


def test_a_zero_major_stable_release_is_only_a_warning() -> None:
    findings = check_version(_manifest(release={"version": "0.9.0"}))
    assert [finding.code for finding in findings] == ["zero_major_on_stable"]
    assert findings[0].severity == "warning"


# --------------------------------------------------------------------------- licenses


def test_known_spellings_normalize_to_spdx() -> None:
    assert normalize_spdx("apache2") == "Apache-2.0"
    assert normalize_spdx("  APACHE   2.0 ") == "Apache-2.0"
    assert normalize_spdx("mit") == "MIT"
    assert normalize_spdx("BSD-3-Clause") == "BSD-3-Clause"


def test_an_empty_identifier_is_refused() -> None:
    with pytest.raises(LicenseRuleError) as raised:
        normalize_spdx("   ")
    assert raised.value.code == "license_invalid"


def test_a_deprecated_identifier_cannot_be_normalized_silently() -> None:
    with pytest.raises(LicenseRuleError) as raised:
        normalize_spdx("GPL-2.0")
    assert raised.value.code == "license_deprecated"


def test_a_deprecated_identifier_is_a_warning_not_a_failure() -> None:
    findings = check_license(
        _manifest(license={"spdx": "GPL-2.0", "files": ["LICENSE"]}),
        allowed=("Apache-2.0", "MIT", "BSD-3-Clause", "GPL-2.0-only"),
    )
    assert [finding.code for finding in findings] == ["license_deprecated"]
    assert findings[0].severity == "warning"
    assert findings[0].subject == "license.spdx"


def test_a_license_outside_the_allow_list_is_an_error() -> None:
    findings = check_license(_manifest(license={"spdx": "MPL-2.0", "files": ["LICENSE"]}))
    assert [finding.code for finding in findings] == ["license_not_allowed"]


def test_the_allow_list_is_compared_after_normalization() -> None:
    findings = check_license(
        _manifest(license={"spdx": "apache2", "files": ["LICENSE"]}),
        allowed=("Apache-2.0",),
    )
    assert findings == ()


def test_a_published_license_needs_a_bundled_file() -> None:
    findings = check_license(_manifest(license={"spdx": "MIT", "files": []}))
    assert [finding.code for finding in findings] == ["license_file_missing"]
    assert findings[0].subject == "license.files"


def test_an_absolute_license_file_is_an_error() -> None:
    findings = check_license(
        _manifest(license={"spdx": "MIT", "files": ["/etc/LICENSE"]}),
        allowed=("MIT",),
    )
    assert [finding.code for finding in findings] == ["license_file_path"]
    assert findings[0].subject == "license.files[0]"


def test_a_traversing_license_file_is_an_error() -> None:
    findings = check_license(
        _manifest(license={"spdx": "MIT", "files": ["../LICENSE"]}),
        allowed=("MIT",),
    )
    assert [finding.code for finding in findings] == ["license_file_path"]


def test_an_unlicensed_release_needs_no_license_file() -> None:
    findings = check_license(
        _manifest(license={"spdx": "UNLICENSED", "files": []}),
        allowed=("UNLICENSED",),
    )
    assert findings == ()
