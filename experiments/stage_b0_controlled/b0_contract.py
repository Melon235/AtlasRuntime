"""Stage B0 frozen contract: controlled real delegation mechanism validation.

What B0 measures
----------------
The *mechanism*, not the policy: with a Host-authored instruction that says this run is validating
the delegation tool, does a real DeepSeek Main call ``delegate_tasks``, does the runtime admit two
independent read-only children, do their executions overlap, does the Main receive bounded summaries
plus refs and resume its own loop, and does the integrated result pass the public suite and the
independent oracle?

What B0 does not measure
------------------------
Whether a capable Main delegates *on its own*. The instruction is experiment scaffolding, so every
result from this stage is labelled ``CONTROLLED_DELEGATION`` and must never be reported as
``AUTONOMOUS_MAIN_DELEGATION``. Stage P1b is the only autonomous sample so far, and it produced no
delegation at all.

Frozen inputs
-------------
The fixture, the public suite and the oracle are the stage P1b artifacts, imported rather than
copied: one fixture, one suite, one oracle, one digest. Only the goal text (and therefore the goal
digest) is B0's own, because the controlled instruction is part of what the Main is told.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
P1B_DIR = REPO_ROOT / "experiments" / "stage_p1b_live"
sys.path.insert(0, str(P1B_DIR))

import p1b_contract as p1b  # noqa: E402

from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED  # noqa: E402

#: Recorded in every artefact of this stage. Never promote this to "autonomous".
DELEGATION_MODE = "CONTROLLED_DELEGATION"

#: The goal-path tool error semantics this run is frozen under (reliability closure, DEV-3).
TOOL_ERROR_MODE = TOOL_ERROR_MODE_CORRECTED

#: The user-facing need. Identical in substance to stage P1b: the same project, the same gap, the
#: same compatibility requirement, the same write scope.
USER_TASK = (
    "The eventpipe project in this workspace ingests events from two channels: a JSON Lines file "
    "channel in plugins/file_events and a chat message channel in plugins/message_events. Its "
    "consumers need one unified normalized event whichever channel a record arrived from. Both "
    "channels already parse their own transport, but the shared path in the core package still "
    "refuses to normalize either of them, so nothing downstream can rely on a single shape.\n"
    "\n"
    "Please finish the unified path inside core so that a record from either channel normalizes "
    "to the same shape, with the existing channel behaviour and the existing suites intact. Only "
    "files inside core may be written. Use the public test suite to check your work."
)

#: The controlled instruction. Host-authored, published with this file, and hashed separately so a
#: reader can see exactly what the Main was told beyond the user's own request.
CONTROLLED_INSTRUCTION = (
    "HOST-AUTHORED EXPERIMENT INSTRUCTION (this run is a runtime validation, not a product "
    "request):\n"
    "This run validates AtlasRuntime's delegation plane. Before you edit any file, call "
    "delegate_tasks exactly once, with two independent read-only research tasks -- one per "
    "plugin package -- each with its own objective and enough background for a worker that cannot "
    "see this "
    "conversation. Then use what they report, do the integration yourself, and run the public test "
    "suite. This instruction is scaffolding: the run is recorded as CONTROLLED_DELEGATION, not as "
    "evidence that an unconstrained agent chose to delegate."
)

GOAL = f"{USER_TASK}\n\n{CONTROLLED_INSTRUCTION}\n"

# ---------------------------------------------------------------- shared contracts

FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "stage_p1b_dual_plugin"
PUBLIC_TESTS = p1b.PUBLIC_TESTS
ORACLE_COMMAND = p1b.ORACLE_COMMAND
acceptance = p1b.acceptance
MAIN_WRITE_PATHS = p1b.MAIN_WRITE_PATHS

# ---------------------------------------------------------------- live configuration
# Every value is the one stage P1b ran with, so B0 changes the instruction and nothing else.

PROVIDER = p1b.PROVIDER
MODEL = p1b.MODEL
IMAGE = p1b.IMAGE
MAIN_MAX_TURNS = p1b.MAIN_MAX_TURNS
TOKEN_BUDGET = p1b.TOKEN_BUDGET
SANDBOX_TIMEOUT_MINUTES = 20
MAIN_SYSTEM_PROMPT_DIGEST = p1b.MAIN_SYSTEM_PROMPT_DIGEST
RESULT_PREVIEW_CHARS = 900
DELEGATION = {
    "max_parallel_children": 2,
    "max_children_per_call": 2,
    "max_delegation_rounds": 4,
    "child_timeout_seconds": 300.0,
    "child_max_turns": 6,
}

#: Exactly one authorised live attempt, and no automatic retry.
LIVE_ATTEMPTS = 1


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def goal_digest() -> str:
    return digest(GOAL)


def instruction_digest() -> str:
    return digest(CONTROLLED_INSTRUCTION)


def frozen_identity() -> dict[str, object]:
    """Everything that must be identical before and after the single live attempt."""

    return {
        "delegation_mode": DELEGATION_MODE,
        "goal": GOAL,
        "goal_digest": goal_digest(),
        "controlled_instruction": CONTROLLED_INSTRUCTION,
        "controlled_instruction_digest": instruction_digest(),
        "user_task_digest": digest(USER_TASK),
        "provider": PROVIDER,
        "model": MODEL,
        "main_system_prompt_digest": MAIN_SYSTEM_PROMPT_DIGEST,
        "main_max_turns": MAIN_MAX_TURNS,
        "token_budget": TOKEN_BUDGET,
        # The reliability-closure semantics this run is validated under. A live B0 run on a runtime
        # whose goal path fell back to ``legacy`` would be measuring a different system.
        "tool_error_mode": TOOL_ERROR_MODE,
        "main_write_paths": list(MAIN_WRITE_PATHS),
        "sandbox": {
            "provider": "opensandbox",
            "image": IMAGE,
            "network": "none",
            "timeout_minutes": SANDBOX_TIMEOUT_MINUTES,
        },
        "delegation": {"enabled": True, **DELEGATION},
        "result_preview_chars": RESULT_PREVIEW_CHARS,
        "public_tests": {
            "test_id": PUBLIC_TESTS.test_id,
            "command": PUBLIC_TESTS.command,
            "command_digest": digest(PUBLIC_TESTS.command),
        },
        "oracle": {
            "command_digest": digest(ORACLE_COMMAND),
            "chars": len(ORACLE_COMMAND),
        },
        "fixture_root": "experiments/fixtures/stage_p1b_dual_plugin",
        "live_attempts": LIVE_ATTEMPTS,
    }
