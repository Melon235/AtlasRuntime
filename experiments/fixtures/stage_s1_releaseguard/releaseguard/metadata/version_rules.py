"""Version syntax, ordering, and the release-channel rules.

The version grammar is the PEP 440 subset this project publishes with::

    MAJOR.MINOR.PATCH [ (a|b|rc) N ] [ +LOCAL ]

so ``2.4.0``, ``2.4.0rc1`` and ``2.5.0b2+build.7`` are versions and ``2.4``, ``2.4.0.1`` and
``2.4.0-rc1`` are not. Ordering follows the same subset: ``a < b < rc < final``, then the numeric
suffix.

The rules below describe what a *release channel* requires of a version. They return findings rather
than raising: a release with one bad rule still has other rules worth reporting, and a caller reads
every finding before deciding anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import Severity, VersionRuleError
from .manifest_parser import ReleaseManifest

#: Channels this project publishes to. ``nightly`` imposes no version-shape rule.
CHANNELS: tuple[str, ...] = ("stable", "candidate", "nightly")

_PRERELEASE_RANK = {"a": 0, "b": 1, "rc": 2}
_RELEASE_RANK = 3

_VERSION_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:(?P<pre_label>a|b|rc)(?P<pre_number>\d+))?"
    r"(?:\+(?P<local>[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*))?$"
)

_PYTHON_REQUIREMENT_RE = re.compile(r"^(>=|==|~=)(?P<major>\d+)\.(?P<minor>\d+)$")


@dataclass(frozen=True)
class Version:
    """A parsed release version."""

    major: int
    minor: int
    patch: int
    prerelease_label: str | None = None
    prerelease_number: int = 0
    local: str | None = None

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease_label is not None

    def _rank(self) -> tuple[int, int]:
        if self.prerelease_label is None:
            return (_RELEASE_RANK, 0)
        return (_PRERELEASE_RANK[self.prerelease_label], self.prerelease_number)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease_label is not None:
            base += f"{self.prerelease_label}{self.prerelease_number}"
        if self.local is not None:
            base += f"+{self.local}"
        return base


@dataclass(frozen=True)
class VersionFinding:
    """One rule violation, in the vocabulary the audit layer reports.

    ``severity`` uses the literal values of the audit contract's severity vocabulary. The metadata
    package does not import the audit package: it must stay usable on its own.
    """

    code: str
    subject: str
    message: str
    severity: Severity = "error"


def parse_version(text: str) -> Version:
    """Parse ``text`` as a release version.

    Raises:
        VersionRuleError: the string is not a version this project can publish (``code`` is
            ``"version_invalid"`` and ``subject`` is the input, so a caller can quote it).
    """

    match = _VERSION_RE.match(text.strip()) if isinstance(text, str) else None
    if match is None:
        raise VersionRuleError(
            f"{text!r} is not a MAJOR.MINOR.PATCH release version",
            code="version_invalid",
            subject=text,
        )
    pre_label = match.group("pre_label")
    return Version(
        major=int(match.group("major")),
        minor=int(match.group("minor")),
        patch=int(match.group("patch")),
        prerelease_label=pre_label,
        prerelease_number=int(match.group("pre_number") or 0),
        local=match.group("local"),
    )


def compare_versions(left: str | Version, right: str | Version) -> int:
    """``-1``, ``0`` or ``1``, ordering two versions of the same release line."""

    first = parse_version(left) if isinstance(left, str) else left
    second = parse_version(right) if isinstance(right, str) else right
    first_key = (first.major, first.minor, first.patch, *first._rank())
    second_key = (second.major, second.minor, second.patch, *second._rank())
    return (first_key > second_key) - (first_key < second_key)


def is_prerelease(text: str) -> bool:
    """Whether ``text`` is a valid version that is not a final release."""

    return parse_version(text).is_prerelease


def check_version(manifest: ReleaseManifest) -> tuple[VersionFinding, ...]:
    """Every version/channel rule the manifest violates, in a stable order.

    Order: syntax, channel, channel-vs-prerelease, Python requirement, then warnings.
    """

    findings: list[VersionFinding] = []
    try:
        version = parse_version(manifest.version)
    except VersionRuleError:
        findings.append(
            VersionFinding(
                code="version_invalid",
                subject="release.version",
                message=f"release.version {manifest.version!r} is not a MAJOR.MINOR.PATCH version",
            )
        )
        version = None

    channel = manifest.channel.strip().lower()
    if channel not in CHANNELS:
        findings.append(
            VersionFinding(
                code="unknown_channel",
                subject="release.channel",
                message=(
                    f"release.channel {manifest.channel!r} is not one of {list(CHANNELS)}"
                ),
            )
        )
    elif version is not None:
        if channel == "stable" and version.is_prerelease:
            findings.append(
                VersionFinding(
                    code="prerelease_on_stable",
                    subject="release.version",
                    message=(
                        f"the stable channel publishes final versions; {version} is a prerelease"
                    ),
                )
            )
        if channel == "candidate" and not version.is_prerelease:
            findings.append(
                VersionFinding(
                    code="candidate_requires_prerelease",
                    subject="release.version",
                    message=(
                        "the candidate channel publishes prereleases; "
                        f"{version} has no a/b/rc suffix"
                    ),
                )
            )
        if channel == "stable" and version.major == 0:
            findings.append(
                VersionFinding(
                    code="zero_major_on_stable",
                    subject="release.version",
                    message=f"the stable channel publishes {version} with a zero major version",
                    severity="warning",
                )
            )

    requirement = manifest.python_requirement.strip()
    match = _PYTHON_REQUIREMENT_RE.match(requirement)
    if match is None:
        findings.append(
            VersionFinding(
                code="python_requirement_invalid",
                subject="release.python_requirement",
                message=(
                    f"release.python_requirement {requirement!r} must look like '>=3.9', '==3.12' "
                    "or '~=3.11'"
                ),
            )
        )
    elif (int(match.group("major")), int(match.group("minor"))) < (3, 9):
        findings.append(
            VersionFinding(
                code="python_requirement_unsupported",
                subject="release.python_requirement",
                message=(
                    f"release.python_requirement {requirement!r} is below the supported floor 3.9"
                ),
            )
        )
    return tuple(findings)


__all__ = [
    "CHANNELS",
    "Severity",
    "Version",
    "VersionFinding",
    "check_version",
    "compare_versions",
    "is_prerelease",
    "parse_version",
]
