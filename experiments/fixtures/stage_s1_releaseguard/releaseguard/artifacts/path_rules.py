"""The one definition of a safe artifact path.

An artifact path is a path *inside* a build directory. It is written with ``/`` separators, it is
relative, it has no parent traversal, it names a file rather than a directory, and every segment is
drawn from a small character set. Backslashes are accepted on input and normalized, because manifests
produced on Windows carry them; anything else is refused with a code that says why.

:func:`normalize_artifact_path` is the only place this rule exists. The scanner applies it to what it
finds on disk and the audit applies it to what the manifest declares, so "declared" and "scanned"
paths are comparable.
"""

from __future__ import annotations

import re

from .errors import ArtifactPathError

#: One path segment. Deliberately narrow: a segment with a space, a colon or a wildcard cannot be
#: published, and refusing it here is cheaper than discovering it in a download URL.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

#: Bytes that must never appear in a published path, even in a segment that looks harmless.
_CONTROL_CHARS = {chr(code) for code in range(0x20)} | {chr(0x7F)}


def normalize_artifact_path(text: str) -> str:
    """Return the canonical relative path that ``text`` denotes.

    Raises:
        ArtifactPathError: ``text`` is not a usable artifact path. The ``code`` names the reason:
            ``path_not_text``, ``path_empty``, ``path_control_character``, ``path_absolute``,
            ``path_escape``, ``path_not_normalized`` or ``path_character``.
    """

    if not isinstance(text, str):
        raise ArtifactPathError(
            f"an artifact path must be a string, got {type(text).__name__}",
            code="path_not_text",
            subject=str(text),
        )
    candidate = text.strip()
    if not candidate:
        raise ArtifactPathError("an artifact path must not be empty", code="path_empty", subject=text)
    if any(char in _CONTROL_CHARS for char in candidate):
        raise ArtifactPathError(
            f"artifact path {text!r} contains a control character",
            code="path_control_character",
            subject=text,
        )

    slashed = candidate.replace("\\", "/")
    if slashed.startswith("/") or (len(slashed) > 1 and slashed[1] == ":"):
        raise ArtifactPathError(
            f"artifact path {text!r} must be relative to the build directory",
            code="path_absolute",
            subject=text,
        )
    if slashed.endswith("/"):
        raise ArtifactPathError(
            f"artifact path {text!r} names a directory, not a file",
            code="path_directory",
            subject=text,
        )

    segments: list[str] = []
    for segment in slashed.split("/"):
        if segment in ("", "."):
            # ``a//b`` and ``./a`` are not canonical: a manifest that writes them is describing a
            # path this tool would not have produced, so it is refused rather than silently fixed.
            raise ArtifactPathError(
                f"artifact path {text!r} is not a normalized relative path",
                code="path_not_normalized",
                subject=text,
            )
        if segment == "..":
            raise ArtifactPathError(
                f"artifact path {text!r} escapes the build directory",
                code="path_escape",
                subject=text,
            )
        if not _SEGMENT_RE.match(segment):
            raise ArtifactPathError(
                f"artifact path {text!r} has a segment {segment!r} outside [A-Za-z0-9._+-]",
                code="path_character",
                subject=text,
            )
        segments.append(segment)
    return "/".join(segments)


def is_safe_artifact_path(text: str) -> bool:
    """Whether :func:`normalize_artifact_path` accepts ``text``. Never raises."""

    try:
        normalize_artifact_path(text)
    except ArtifactPathError:
        return False
    return True


__all__ = ["is_safe_artifact_path", "normalize_artifact_path"]
