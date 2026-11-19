"""The shared release readiness audit.

This is the only module that knows a manifest and a build directory exist at the same time. It reads
both sides, compares them, and returns a report built by
:func:`releaseguard.audit.report.build_report`.

The sequence, and what each step owns:

1. ``metadata.manifest`` — parse the manifest. A parse failure is terminal for everything that needs
   a declaration: the version and license checks are ``skip``, and so are ``integration.coverage``
   and ``integration.extras``. The build directory is still inventoried, because "the manifest is
   broken" and "the build directory is empty" are different problems and a run should show both.
2. ``metadata.version`` / ``metadata.license`` — run the metadata rules and turn each finding into an
   issue. A rule that produced no finding is a pass. A metadata failure raised instead of returned
   (an unparseable license identifier, for instance) becomes an issue with the error's own ``code``.
3. ``artifacts.inventory`` — scan the build directory. If the scan itself fails, that is a failed
   check and every remaining artifact and integration check is ``skip``: there is nothing to compare
   against.
4. ``artifacts.paths`` — apply the artifact path rule to every declared path, and report every entry
   the scanner refused. Anything in this check is an error.
5. ``artifacts.checksums`` — for each declared artifact that the inventory contains, compare the
   declared digest and size with the scanned ones. A malformed declared digest, a digest mismatch and
   a size mismatch are three different issues and three different codes.
6. ``integration.coverage`` — a declared artifact the inventory does not contain. This is where
   "missing file" is reported; the checksum check stays silent about it. A declaration whose path is
   not a usable artifact path is reported by the path check and is not also reported as missing: one
   mistake must not produce two findings.
7. ``integration.extras`` — a file in the build directory the manifest does not declare. Severity
   follows ``AuditPolicy.allow_extra_artifacts``: a warning when extras are tolerated, an error
   otherwise.

Every check is always present in the report, in :data:`~releaseguard.audit.contracts.CHECK_ORDER`,
including the ones that were skipped. ``artifact_count`` is the number of entries the inventory
accepted.

Issue subjects are stable identifiers a consumer can key on: a dotted metadata key
(``"release.version"``), an ``artifacts[i]`` index, or the artifact's relative path as written in the
manifest.
"""

from __future__ import annotations

from pathlib import Path

from .contracts import DEFAULT_POLICY, AuditPolicy, AuditReport


def audit_release(
    manifest_path: str | Path,
    artifact_root: str | Path,
    *,
    policy: AuditPolicy | None = None,
) -> AuditReport:
    """Audit the release described by ``manifest_path`` against ``artifact_root``.

    Both paths are read; neither is written. ``policy`` defaults to
    :data:`~releaseguard.audit.contracts.DEFAULT_POLICY`.
    """

    raise NotImplementedError("the shared release audit is not implemented yet")


__all__ = ["audit_release"]
