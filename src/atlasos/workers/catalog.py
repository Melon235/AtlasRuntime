"""Trusted WorkerCatalog. Profiles are immutable Python definitions."""

from __future__ import annotations

from collections.abc import Mapping

from atlasos.workers.models import WorkerConfigurationError, WorkerProfile

RESEARCH_INSTRUCTIONS = (
    "Investigate the assigned question using evidence. Prefer read-only discovery and "
    "cite concrete observations. Do not take external side effects without explicit approval."
)
#: Version label of the coding completion contract. A run records the digest of the instructions it
#: rendered, so a change here is visible in the evidence rather than inferred from a prompt hash.
CODING_CONTRACT_VERSION = "c1b-1.0"

CODING_INSTRUCTIONS = (
    "Implement the assigned change inside the files you are authorized to write. Keep edits "
    "focused, preserve behavior outside the task, and do not perform external communication.\n"
    "\n"
    "Use the tools you were given. A deterministic refusal is information, not a transient error: "
    "do not retry the refused command or an equivalent form of it.\n"
    "\n"
    "Verify with what is actually available: read back what you wrote, inspect call sites, and "
    "reason against the published interface. When a command is refused by policy, record the "
    "requirement as unverified and leave project-level test execution to the Main.\n"
    "\n"
    "Do not create scratch files outside your authorized paths, and never claim a test result you "
    "did not obtain.\n"
    "\n"
    "Finish when the implementation and a targeted local review are done, or when a real blocker "
    "is reached, and end with a short report covering: completion status, changed files, behavior "
    "implemented, interface or integration notes, verification performed, and unverified "
    "requirements or known limitations."
)
DATA_INSTRUCTIONS = (
    "Inspect and reason about data using read-oriented tools. Keep queries scoped to the task. "
    "Do not mutate durable data unless the task explicitly requires it."
)
GENERAL_INSTRUCTIONS = (
    "Complete the assigned task with the available basic tools. Ask for a more focused worker "
    "when the task requires a specialty; do not assume extra authority."
)

DEFAULT_PROFILES: tuple[WorkerProfile, ...] = (
    WorkerProfile(
        profile_id="research_worker",
        role="research and diagnosis worker",
        instructions=RESEARCH_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("research", "diagnostics"),
        max_turns=6,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="coding_worker",
        role="coding and diagnostics worker",
        instructions=CODING_INSTRUCTIONS,
        base_tools=(
            "tool_search",
            "dispatch_action",
            "file_read",
            "file_write",
            "file_edit",
            "grep",
            "list_files",
            "bash",
        ),
        operating_scope=("coding",),
        max_turns=8,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="data_worker",
        role="data inspection worker",
        instructions=DATA_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("data",),
        max_turns=6,
        can_delegate=False,
    ),
    WorkerProfile(
        profile_id="general_worker",
        role="general fallback worker",
        instructions=GENERAL_INSTRUCTIONS,
        base_tools=("tool_search", "dispatch_action", "file_read", "grep", "list_files"),
        operating_scope=("general",),
        max_turns=4,
        can_delegate=False,
    ),
)


class WorkerCatalog:
    def __init__(self, profiles: tuple[WorkerProfile, ...] = DEFAULT_PROFILES) -> None:
        self._profiles = {profile.profile_id: profile for profile in profiles}
        if len(self._profiles) != len(profiles):
            raise ValueError("duplicate worker profile_id")

    @property
    def profiles(self) -> Mapping[str, WorkerProfile]:
        return dict(self._profiles)

    def resolve(self, profile_id: str) -> WorkerProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise WorkerConfigurationError(f"unknown worker profile {profile_id!r}") from error
