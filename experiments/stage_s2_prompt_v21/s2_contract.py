"""Stage S2 frozen contract: Main System Prompt v2.1 final pilot.

What S2 measures
----------------
Three behaviours that Stage S1 exposed, on a coding task the model has never seen, with **no**
instruction anywhere telling it to delegate:

1. does it decide Direct vs Delegated before it has already done the investigation;
2. does it pick work the chosen worker profile can actually perform;
3. does it reach implementation and the public suite inside the budget.

What S2 does not measure
------------------------
Any causal effect, and any token-efficiency comparison against S1. The task, the budget and the turn
cap all differ, so cross-stage numbers are descriptions, not measurements.

Frozen inputs
-------------
The prompt text is a file and its bytes are the identity. The hidden acceptance program is stored on
the host and travels into the sandbox only inside the verification command, base64-encoded, so it
cannot be mistaken for a repository file.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from atlasos.app.goal_verification import GoalAcceptance
from atlasos.app.main_driven import MAIN_SYSTEM_PROMPT
from atlasos.app.public_tests import PublicTestContract
from atlasos.sandbox.tools import TOOL_ERROR_MODE_CORRECTED
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

# -- the prompt under test ---------------------------------------------------

PROMPT_V21_PATH = HERE / "prompts" / "main_v21.md"
PROMPT_V21_VERSION = "main-v2.1-rc1"
#: ``sha256`` of the prompt **text** — the string that becomes ``AgentConfig.system_prompt``.
#:
#: Text, not file bytes: a checkout may materialise the file with CRLF endings, and an identity
#: that moves with a working tree's line-ending convention is not an identity. The file is stored
#: with LF, so on this tree the two digests coincide; the file digest is recorded separately.
PROMPT_V21_DIGEST = "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489"

#: Stage S1's prompt, frozen here as the immediate predecessor. Unchanged file, unchanged digest.
PROMPT_V2_VERSION = "main-v2.0-rc1"
PROMPT_V2_DIGEST = "sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323"
PROMPT_V2_PATH = REPO_ROOT / "experiments" / "stage_s1_prompt_v2" / "prompts" / "main_v2.md"

#: Stage P0.1's prompt, still the runtime default and unchanged by this stage.
PROMPT_V1_VERSION = "main-v1.0"
PROMPT_V1_DIGEST = "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a"

# -- the fixture -------------------------------------------------------------

FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "stage_s2_workgrid"
FIXTURE_SKIP = {"__pycache__", ".pytest_cache", ".git", "build.py"}
#: Files the reference implementation replaces. The Host keeps them; the sandbox never does.
STUB_FILES = ("workgrid/scheduler/engine.py", "workgrid/scheduler/report.py")
REFERENCE_ROOT = HERE / "reference"

# -- the Main-facing goal ----------------------------------------------------
#: A user's request, nothing else. No "you must delegate", no worker count, no hint that this run is
#: measuring anything, and no pointer at which package to investigate first.
GOAL = (
    "The WorkGrid project already provides dependency graph validation and resource availability "
    "rules, but its shared scheduling engine is incomplete. Implement a deterministic scheduling "
    "workflow that respects task dependencies and resource capacity constraints, reports "
    "unschedulable tasks clearly, and preserves the existing dependency and resource APIs. Make "
    "changes only in the authorized scheduler package and use the available public tests to verify "
    "the result."
)

#: Phrases that would turn this pilot back into a controlled experiment. Their absence is a blocking
#: preflight check, not a convention.
FORBIDDEN_GOAL_PHRASES: tuple[str, ...] = (
    "you must delegate",
    "call delegate_tasks",
    "create two workers",
    "investigate dependencies and resources in parallel",
    "this run evaluates your delegation behavior",
    "use subworkers before editing",
    "host-authored experiment instruction",
    "always delegate",
    "every task requires subagents",
)

# -- live configuration ------------------------------------------------------

PROVIDER = "deepseek"
MODEL = "deepseek-chat"
IMAGE = "atlas-uat-python:3.12"
MAIN_MAX_TURNS = 20
TOKEN_BUDGET = 500_000
SANDBOX_TIMEOUT_MINUTES = 30
#: The whole shared scheduling layer, and nothing else. The dependency and resource packages are a
#: compatibility surface: a write there is denied by the ownership policy, not by a prompt.
MAIN_WRITE_PATHS: tuple[str, ...] = ("workgrid/scheduler/",)
DELEGATION = {
    "max_parallel_children": 2,
    "max_children_per_call": 2,
    "max_delegation_rounds": 3,
    "child_timeout_seconds": 300.0,
    "child_max_turns": 6,
}
RESULT_PREVIEW_CHARS = 900
TOOL_ERROR_MODE = TOOL_ERROR_MODE_CORRECTED

#: Exactly one authorised live attempt, and no automatic retry.
LIVE_ATTEMPTS = 1

# -- the public channel (the Main may run this) ------------------------------

PUBLIC_TESTS = PublicTestContract(
    test_id="public",
    command="python3 -B -m pytest -q tests",
    timeout_seconds=240,
    max_output_chars=6000,
    max_calls=4,
)

# -- the hidden oracle -------------------------------------------------------

#: The acceptance program, kept on the Host. It is run inside the sandbox by the verification step,
#: encoded so that shell quoting cannot alter it and so it is not a file the agent could read.
HIDDEN_PROGRAM = (REFERENCE_ROOT / "oracle_program.py").read_text(encoding="utf-8")
HIDDEN_ENCODED = base64.b64encode(HIDDEN_PROGRAM.encode("utf-8")).decode("ascii")
#: ``pytest`` first (the whole suite, including both compatibility suites), then the hidden program.
#: Joined with ``&&`` so a failing suite can never be reported as an oracle pass.
ORACLE_COMMAND = (
    'python3 -m pytest -q && python3 -c "import base64;'
    f"exec(base64.b64decode('{HIDDEN_ENCODED}').decode())\""
)


def acceptance() -> GoalAcceptance:
    spec = VerificationSpec(
        spec_id="stage-s2-workgrid",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=(
            "workgrid/scheduler/engine.py",
            "workgrid/scheduler/report.py",
        ),
        checks=(
            CheckSpec(
                check_id="engine-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="workgrid/scheduler/engine.py",
            ),
            CheckSpec(
                check_id="report-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="workgrid/scheduler/report.py",
            ),
            CheckSpec(
                check_id="engine-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="workgrid/scheduler/engine.py",
            ),
            CheckSpec(
                check_id="report-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="workgrid/scheduler/report.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(spec=spec, dynamic_command=ORACLE_COMMAND, dynamic_timeout_seconds=420)


# -- reference implementation (host side only) -------------------------------


def reference_texts() -> dict[str, str]:
    return {
        "workgrid/scheduler/engine.py": (REFERENCE_ROOT / "engine_reference.py").read_text(
            encoding="utf-8"
        ),
        "workgrid/scheduler/report.py": (REFERENCE_ROOT / "report_reference.py").read_text(
            encoding="utf-8"
        ),
    }


def apply_reference(root: Path) -> None:
    """Install the reference implementation into a *scratch* copy. Never called on the fixture."""

    for relative, text in reference_texts().items():
        target = root / relative
        target.write_text(text, encoding="utf-8")


# -- identity ----------------------------------------------------------------


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def goal_digest() -> str:
    return digest(GOAL)


def prompt_v21_text() -> str:
    return PROMPT_V21_PATH.read_text(encoding="utf-8")


def prompt_v21_digest() -> str:
    return digest(prompt_v21_text())


def prompt_v21_file_digest() -> str:
    return digest_bytes(PROMPT_V21_PATH.read_bytes())


def prompt_v2_text() -> str:
    return PROMPT_V2_PATH.read_text(encoding="utf-8")


def prompt_v2_digest() -> str:
    return digest(prompt_v2_text())


def prompt_v2_file_digest() -> str:
    return digest_bytes(PROMPT_V2_PATH.read_bytes())


def forbidden_goal_phrase() -> str | None:
    """The first controlled-experiment phrase found in the goal, or ``None``."""

    lowered = GOAL.lower()
    for phrase in FORBIDDEN_GOAL_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def forbidden_prompt_phrase() -> str | None:
    """The first controlled-experiment phrase found in the prompt under test, or ``None``."""

    lowered = prompt_v21_text().lower()
    for phrase in FORBIDDEN_GOAL_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def frozen_identity() -> dict[str, object]:
    """Everything that must be identical before and after the single live attempt."""

    return {
        "stage": "S2",
        "experiment": "main_system_prompt_v21_final_pilot",
        "delegation_mode": "AUTONOMOUS_MAIN_DELEGATION",
        "goal": GOAL,
        "goal_digest": goal_digest(),
        "prompt": {
            "selected_version": PROMPT_V21_VERSION,
            "selected_digest": PROMPT_V21_DIGEST,
            "selected_digest_scope": "sha256 of the prompt text (AgentConfig.system_prompt)",
            "path": "experiments/stage_s2_prompt_v21/prompts/main_v21.md",
            "text_chars": len(prompt_v21_text()),
            "file_bytes": len(PROMPT_V21_PATH.read_bytes()),
            "file_digest": prompt_v21_file_digest(),
            "previous_version": PROMPT_V2_VERSION,
            "previous_digest": PROMPT_V2_DIGEST,
            "previous_path": "experiments/stage_s1_prompt_v2/prompts/main_v2.md",
            "previous_file_digest": prompt_v2_file_digest(),
            "default_version": PROMPT_V1_VERSION,
            "default_digest": PROMPT_V1_DIGEST,
            "default_text_chars": len(MAIN_SYSTEM_PROMPT),
        },
        "provider": PROVIDER,
        "model": MODEL,
        "main_max_turns": MAIN_MAX_TURNS,
        "token_budget": TOKEN_BUDGET,
        "tool_error_mode": TOOL_ERROR_MODE,
        "main_write_paths": list(MAIN_WRITE_PATHS),
        "sandbox": {
            "provider": "opensandbox",
            "image": IMAGE,
            "network": "none",
            "cpu": "1",
            "memory": "1Gi",
            "timeout_minutes": SANDBOX_TIMEOUT_MINUTES,
        },
        "delegation": {"enabled": True, **DELEGATION},
        "result_preview_chars": RESULT_PREVIEW_CHARS,
        "public_tests": {
            "test_id": PUBLIC_TESTS.test_id,
            "command": PUBLIC_TESTS.command,
            "command_digest": PUBLIC_TESTS.command_digest(),
            "max_calls": PUBLIC_TESTS.max_calls,
            "timeout_seconds": PUBLIC_TESTS.timeout_seconds,
        },
        "oracle": {
            "command_digest": digest(ORACLE_COMMAND),
            "chars": len(ORACLE_COMMAND),
            "program_digest": digest(HIDDEN_PROGRAM),
            "program_chars": len(HIDDEN_PROGRAM),
        },
        "fixture_root": "experiments/fixtures/stage_s2_workgrid",
        "live_attempts": LIVE_ATTEMPTS,
    }


__all__ = [
    "DELEGATION",
    "FIXTURE_ROOT",
    "FIXTURE_SKIP",
    "FORBIDDEN_GOAL_PHRASES",
    "GOAL",
    "HIDDEN_PROGRAM",
    "IMAGE",
    "LIVE_ATTEMPTS",
    "MAIN_MAX_TURNS",
    "MAIN_WRITE_PATHS",
    "MODEL",
    "ORACLE_COMMAND",
    "PROMPT_V1_DIGEST",
    "PROMPT_V1_VERSION",
    "PROMPT_V21_DIGEST",
    "PROMPT_V21_PATH",
    "PROMPT_V21_VERSION",
    "PROMPT_V2_DIGEST",
    "PROMPT_V2_PATH",
    "PROMPT_V2_VERSION",
    "PROVIDER",
    "PUBLIC_TESTS",
    "REFERENCE_ROOT",
    "RESULT_PREVIEW_CHARS",
    "SANDBOX_TIMEOUT_MINUTES",
    "STUB_FILES",
    "TOKEN_BUDGET",
    "TOOL_ERROR_MODE",
    "acceptance",
    "apply_reference",
    "digest",
    "digest_bytes",
    "forbidden_goal_phrase",
    "forbidden_prompt_phrase",
    "frozen_identity",
    "goal_digest",
    "prompt_v21_digest",
    "prompt_v21_file_digest",
    "prompt_v21_text",
    "prompt_v2_digest",
    "prompt_v2_file_digest",
    "prompt_v2_text",
    "reference_texts",
]
