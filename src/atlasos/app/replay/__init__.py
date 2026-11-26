"""Stage H1 read-only historical replay workbench (presentation layer only)."""

from atlasos.app.replay.api import create_replay_router
from atlasos.app.replay.service import (
    EVIDENCE_ROOT_ENV,
    ReplayNotConfigured,
    ReplayRunNotFound,
    ReplayService,
)
from atlasos.app.replay.sources import (
    EvidenceReader,
    EvidenceRoot,
    EvidenceTooLarge,
    PathOutsideRoot,
    ReplayConfigError,
    ReplayError,
    parse_root_specs,
)

__all__ = [
    "EVIDENCE_ROOT_ENV",
    "EvidenceReader",
    "EvidenceRoot",
    "EvidenceTooLarge",
    "PathOutsideRoot",
    "ReplayConfigError",
    "ReplayError",
    "ReplayNotConfigured",
    "ReplayRunNotFound",
    "ReplayService",
    "create_replay_router",
    "parse_root_specs",
]
