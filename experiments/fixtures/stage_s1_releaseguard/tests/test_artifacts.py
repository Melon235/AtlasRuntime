"""The artifact layer: digests, path safety, and the inventory walk.

These tests describe the behaviour the audit is built on: a digest has one form, a path has one
rule, and an inventory is the same value every time it is taken. They pass today and must keep
passing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from releaseguard.artifacts import (
    ArtifactPathError,
    ArtifactScanError,
    DigestFormatError,
    digests_equal,
    is_safe_artifact_path,
    normalize_artifact_path,
    normalize_digest,
    scan_artifacts,
    sha256_bytes,
    sha256_file,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
DIST = SAMPLES / "dist"


# --------------------------------------------------------------------------- digests


def test_a_bare_digest_is_returned_unchanged() -> None:
    assert normalize_digest("a" * 64) == "a" * 64


def test_a_prefixed_or_uppercase_digest_normalizes() -> None:
    assert normalize_digest("sha256:" + "A" * 64) == "a" * 64
    assert normalize_digest("  SHA256:" + "b" * 64 + "  ") == "b" * 64


@pytest.mark.parametrize(
    "text", ["", "a" * 63, "a" * 65, "z" * 64, "md5:" + "a" * 32, "sha512:" + "a" * 128]
)
def test_a_string_that_is_not_a_sha256_digest_is_refused(text: str) -> None:
    with pytest.raises(DigestFormatError) as raised:
        normalize_digest(text)
    assert raised.value.code == "digest_format"


def test_digests_compare_across_spellings() -> None:
    assert digests_equal("sha256:" + "c" * 64, "C" * 64)
    assert not digests_equal("c" * 64, "d" * 64)
    # A malformed digest is never equal to anything, not even to itself.
    assert not digests_equal("nope", "nope")


def test_the_empty_payload_has_the_documented_digest() -> None:
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_a_file_digest_matches_the_bytes_on_disk() -> None:
    target = DIST / "CHANGELOG.md"
    assert sha256_file(target) == hashlib.sha256(target.read_bytes()).hexdigest()


def test_an_unreadable_file_is_a_scan_failure(tmp_path: Path) -> None:
    with pytest.raises(ArtifactScanError) as raised:
        sha256_file(tmp_path / "absent.bin")
    assert raised.value.code == "artifact_read_failed"


# --------------------------------------------------------------------------- paths


def test_a_plain_relative_path_is_kept() -> None:
    assert normalize_artifact_path("releaseguard-2.4.0-py3-none-any.whl") == (
        "releaseguard-2.4.0-py3-none-any.whl"
    )
    assert normalize_artifact_path("dist/CHANGELOG.md") == "dist/CHANGELOG.md"


def test_windows_separators_are_normalized() -> None:
    assert normalize_artifact_path("dist\\CHANGELOG.md") == "dist/CHANGELOG.md"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("", "path_empty"),
        ("   ", "path_empty"),
        ("/etc/passwd", "path_absolute"),
        ("C:/release/a.whl", "path_absolute"),
        ("../a.whl", "path_escape"),
        ("dist/../../a.whl", "path_escape"),
        ("dist/", "path_directory"),
        ("dist//a.whl", "path_not_normalized"),
        ("./a.whl", "path_not_normalized"),
        ("dist/my file.whl", "path_character"),
        ("dist/a.whl\x00", "path_control_character"),
    ],
)
def test_an_unsafe_path_is_refused_with_a_code(text: str, code: str) -> None:
    with pytest.raises(ArtifactPathError) as raised:
        normalize_artifact_path(text)
    assert raised.value.code == code
    assert is_safe_artifact_path(text) is False


def test_a_safe_path_is_recognized_without_raising() -> None:
    assert is_safe_artifact_path("dist/a.whl") is True


# --------------------------------------------------------------------------- inventory


def test_the_sample_build_directory_is_inventoried_in_path_order() -> None:
    inventory = scan_artifacts(DIST)

    assert inventory.paths() == (
        "CHANGELOG.md",
        "extra-debug.log",
        "releaseguard-2.4.0-py3-none-any.whl",
        "releaseguard-2.4.0.tar.gz",
    )
    assert inventory.skipped == ()
    wheel = inventory.by_path()["releaseguard-2.4.0-py3-none-any.whl"]
    assert wheel.size == (DIST / "releaseguard-2.4.0-py3-none-any.whl").stat().st_size
    assert wheel.sha256 == sha256_file(DIST / "releaseguard-2.4.0-py3-none-any.whl")
    assert wheel.is_symlink is False


def test_the_inventory_of_the_same_directory_is_identical_twice() -> None:
    assert scan_artifacts(DIST).as_dict() == scan_artifacts(DIST).as_dict()


def test_a_missing_build_directory_is_a_scan_failure(tmp_path: Path) -> None:
    with pytest.raises(ArtifactScanError) as raised:
        scan_artifacts(tmp_path / "absent")
    assert raised.value.code == "artifact_root_missing"


def test_build_tooling_directories_are_excluded(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("keep\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "keep.cpython-312.pyc").write_bytes(b"\x00\x01")

    assert scan_artifacts(tmp_path).paths() == ("keep.txt",)


def test_a_file_whose_name_is_not_a_safe_artifact_path_is_refused_not_dropped(
    tmp_path: Path,
) -> None:
    (tmp_path / "fine.txt").write_text("fine\n", encoding="utf-8")
    (tmp_path / "not fine.txt").write_text("space\n", encoding="utf-8")

    inventory = scan_artifacts(tmp_path)

    assert inventory.paths() == ("fine.txt",)
    assert [item.code for item in inventory.skipped] == ["path_character"]
    assert inventory.skipped[0].path == "not fine.txt"


def test_a_nested_directory_is_inventoried_with_its_relative_path(tmp_path: Path) -> None:
    nested = tmp_path / "dist" / "wheel"
    nested.mkdir(parents=True)
    (nested / "pkg.whl").write_text("payload\n", encoding="utf-8")

    assert scan_artifacts(tmp_path).paths() == ("dist/wheel/pkg.whl",)
