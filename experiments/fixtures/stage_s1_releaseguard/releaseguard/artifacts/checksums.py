"""SHA-256 helpers.

A digest is stored and compared in one form only: 64 lowercase hex characters, no prefix. Manifests
in the wild write ``sha256:...``, ``SHA256:...`` or the bare digest, so :func:`normalize_digest`
accepts the prefixed spellings on input and every function here returns the bare form.

Files are hashed in chunks: a release can contain an artifact larger than the memory this tool is
allowed to use.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .errors import ArtifactScanError, DigestFormatError

#: Accepted prefixes, case-insensitive. ``sha256`` only: mixing algorithms in one field is how a
#: comparison silently becomes meaningless.
DIGEST_PREFIXES: tuple[str, ...] = ("sha256:",)

_READ_CHUNK = 1024 * 1024
_HEX = set("0123456789abcdef")


def normalize_digest(text: str) -> str:
    """Return the bare lowercase SHA-256 digest that ``text`` denotes.

    Raises:
        DigestFormatError: the text is not a SHA-256 digest in any accepted spelling
            (``code`` is ``"digest_format"``).
    """

    if not isinstance(text, str):
        raise DigestFormatError(
            f"a digest must be a string, got {type(text).__name__}", code="digest_format"
        )
    candidate = text.strip()
    lowered = candidate.lower()
    for prefix in DIGEST_PREFIXES:
        if lowered.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    candidate = candidate.lower()
    if len(candidate) != 64 or not set(candidate) <= _HEX:
        raise DigestFormatError(
            f"{text!r} is not a SHA-256 digest: expected 64 hexadecimal characters",
            code="digest_format",
        )
    return candidate


def digests_equal(left: str, right: str) -> bool:
    """Whether two spellings denote the same digest. A malformed digest is never equal to anything."""

    try:
        return normalize_digest(left) == normalize_digest(right)
    except DigestFormatError:
        return False


def sha256_bytes(payload: bytes) -> str:
    """Bare lowercase SHA-256 of ``payload``."""

    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: str | Path) -> str:
    """Bare lowercase SHA-256 of the file at ``path``.

    Raises:
        ArtifactScanError: the file cannot be read (``code`` is ``"artifact_read_failed"``).
    """

    digest = hashlib.sha256()
    target = Path(path)
    try:
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
                digest.update(chunk)
    except OSError as error:
        raise ArtifactScanError(
            f"cannot read {target}: {error}",
            code="artifact_read_failed",
            subject=str(target),
        ) from error
    return digest.hexdigest()


__all__ = [
    "DIGEST_PREFIXES",
    "digests_equal",
    "normalize_digest",
    "sha256_bytes",
    "sha256_file",
]
