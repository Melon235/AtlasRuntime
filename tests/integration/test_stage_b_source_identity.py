"""Stage B S05 review: the harness source identity is content, not carriage returns.

``source_identity()`` digests ten files by content. Before S05 it hashed raw bytes, so the identity
of one commit changed when ``git add`` re-rendered the worktree: measured ``8980192b…`` (CRLF) and
``39d93903…`` (mixed) for the same code. A freeze pinned either value would not reproduce on a fresh
clone, which is not a usable identity for a live run.

These tests are pure-bytes: no provider, no sandbox, no cell.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from experiments.stage_benchmark.execution import admission  # noqa: E402

EXECUTION_DIR = REF = Path(REPO_ROOT) / "experiments" / "stage_benchmark" / "execution"


def _live_contents() -> dict[str, bytes]:
    return {name: (EXECUTION_DIR / name).read_bytes() for name in admission.SOURCE_FILES}


def _to_lf(contents: dict[str, bytes]) -> dict[str, bytes]:
    return {name: data.replace(b"\r\n", b"\n") for name, data in contents.items()}


def _to_crlf(contents: dict[str, bytes]) -> dict[str, bytes]:
    return {
        name: (data if b"\r\n" in data else data.replace(b"\n", b"\r\n"))
        for name, data in contents.items()
    }


def test_crlf_and_lf_renderings_of_the_same_files_share_one_digest() -> None:
    contents = _live_contents()
    lf = _to_lf(contents)
    crlf = _to_crlf(lf)
    assert crlf != lf  # the two renderings really are different bytes
    assert admission.source_files_digest(crlf) == admission.source_files_digest(lf)


def test_the_live_identity_is_reproducible_from_a_pure_lf_and_a_pure_crlf_copy() -> None:
    contents = _live_contents()
    live = admission.source_identity()["digest"]
    assert live == admission.source_files_digest(_to_lf(contents))
    assert live == admission.source_files_digest(_to_crlf(contents))
    assert live == admission.source_files_digest(contents)


def test_a_real_content_change_changes_the_digest() -> None:
    contents = _to_lf(_live_contents())
    mutated = dict(contents)
    name = "adapter.py"
    mutated[name] = contents[name] + b"\n# one comment line\n"
    assert admission.source_files_digest(mutated) != admission.source_files_digest(contents)

    # A single byte is enough, even inside a CRLF-only difference.
    subtle = dict(contents)
    subtle[name] = contents[name].replace(b"return", b"returN", 1)
    assert admission.source_files_digest(subtle) != admission.source_files_digest(contents)


def test_a_different_file_set_is_a_different_identity() -> None:
    contents = _to_lf(_live_contents())
    renamed = {
        (name if name != "network.py" else "network2.py"): data
        for name, data in contents.items()
    }
    dropped = {name: data for name, data in contents.items() if name != "smoke.py"}
    base = admission.source_files_digest(contents)
    assert admission.source_files_digest(renamed) != base
    assert admission.source_files_digest(dropped) != base


def test_normalisation_is_idempotent_and_keeps_lone_line_feeds() -> None:
    payload = b"a\r\nb\nc\r\n"
    once = admission.normalise_line_endings(payload)
    assert once == b"a\nb\nc\n"
    assert admission.normalise_line_endings(once) == once


def test_the_identity_lists_every_declared_source_file() -> None:
    identity = admission.source_identity()
    assert identity["file_count"] == len(admission.SOURCE_FILES)
    assert set(identity["files"]) == set(admission.SOURCE_FILES)
    assert identity["line_endings"] == "crlf-normalised"
    for name, digest in identity["files"].items():
        expected = "sha256:" + hashlib.sha256(
            admission.normalise_line_endings((EXECUTION_DIR / name).read_bytes())
        ).hexdigest()
        assert digest == expected
