"""Stage S2: the frozen v2.1 candidate is the prompt the Main actually receives.

Every test drives the real ``run_goal`` path with a scripted provider, so what is asserted is the
text ``AgentConfig`` carries, not a mocked seam. The prompt is read from the frozen file rather than
duplicated here: a copy in the test would let the experiment and the test drift apart.

What a scripted provider proves is the interface and the bytes. It does not prove that a real model
decides anything, and nothing here is presented as such.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest
from anycode.types import (
    LLMChatOptions,
    LLMMessage,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from atlasos.app.main_driven import (
    MAIN_PROMPT_V1,
    MAIN_SYSTEM_PROMPT,
    GoalRunOptions,
    MainPromptSpec,
    run_goal,
)
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation import DelegationConfig

STAGE_DIR = Path(__file__).resolve().parents[2] / "experiments" / "stage_s2_prompt_v21"
PROMPT_V21_PATH = STAGE_DIR / "prompts" / "main_v21.md"
PROMPT_V2_PATH = (
    Path(__file__).resolve().parents[2]
    / "experiments"
    / "stage_s1_prompt_v2"
    / "prompts"
    / "main_v2.md"
)

#: ``sha256`` of ``MAIN_SYSTEM_PROMPT`` as frozen by stage P0.1 and reused by every stage since. If
#: this test fails, an older stage is no longer reproducible from this source tree.
FROZEN_V1_DIGEST = "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a"
#: Stage S1's candidate. S2 must not have edited it.
FROZEN_V2_DIGEST = "sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323"
#: Stage S2's candidate.
FROZEN_V21_DIGEST = "sha256:4147f19cab445241f79dde4a03db69a1a96961d775ec6e0da35dc24cb7cdd489"

Response = Callable[[list[LLMMessage]], list[Any]]


def prompt_v21_text() -> str:
    return PROMPT_V21_PATH.read_text(encoding="utf-8")


class PromptRig:
    """Scripted provider that records the exact system prompt of every request."""

    def __init__(self, main_prompt_text: str) -> None:
        self.main_prompt_text = main_prompt_text
        self.main_script: list[Response] = []
        self.child_scripts: dict[str, list[Response]] = {}
        self.requests: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self.name = "stage-s2-scripted"

    def build(self) -> PromptRig:
        return self

    def _role(self, system_prompt: str | None) -> str:
        text = system_prompt or ""
        if text == self.main_prompt_text:
            return "main"
        for marker in self.child_scripts:
            if marker in text:
                return marker
        return "unknown"

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        role = self._role(options.system_prompt)
        self.requests.append(
            {
                "role": role,
                "system_prompt": options.system_prompt,
                "messages": [message.model_dump(mode="json") for message in messages],
            }
        )
        turn = self.calls.get(role, 0)
        self.calls[role] = turn + 1
        script = self.main_script if role == "main" else self.child_scripts.get(role, [])
        blocks: list[Any] = (
            script[turn](messages) if turn < len(script) else [TextBlock(text="done")]
        )
        has_tool_use = any(isinstance(block, ToolUseBlock) for block in blocks)
        return LLMResponse(
            id=f"{role}-{turn}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if has_tool_use else "end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
        )

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        async def _gen():
            response = await self.chat(messages, options)
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                elif isinstance(block, TextBlock):
                    yield StreamEvent(type="text", data=block.text)
            yield StreamEvent(type="done", data=response)

        return _gen()

    def main_system_prompts(self) -> list[str]:
        return [item["system_prompt"] or "" for item in self.requests if item["role"] == "main"]

    def child_system_prompts(self) -> list[str]:
        return [item["system_prompt"] or "" for item in self.requests if item["role"] != "main"]


def _text(text: str) -> Response:
    return lambda _: [TextBlock(text=text)]


def _delegate_call(tasks: list[dict[str, Any]]) -> Response:
    def respond(_: list[LLMMessage]) -> list[Any]:
        return [ToolUseBlock(id="call-delegate", name="delegate_tasks", input={"tasks": tasks})]

    return respond


def _task(key: str, objective: str) -> dict[str, Any]:
    return {
        "key": key,
        "objective": objective,
        "background": "The workspace is a small Python project at the repository root.",
        "profile_id": "research_worker",
    }


def _workspace(root: Path) -> Path:
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return workspace


GOAL = "Add a subtract function and report what arithmetic exists."


async def _run(root: Path, rig: PromptRig, **overrides: Any) -> dict[str, Any]:
    options = GoalRunOptions(
        goal=GOAL,
        runs_root=root / "runs",
        workspace_root=_workspace(root),
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        provider_factory=rig.build,
        delegation=DelegationConfig(),
        run_id=f"stage-s2-{uuid.uuid4().hex[:10]}",
        **overrides,
    )
    return await run_goal(options)


def _run_in(root: Path, rig: PromptRig, **overrides: Any) -> Coroutine[Any, Any, dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    return _run(root, rig, **overrides)


# --------------------------------------------------------------------------- file identities


def test_both_prompt_files_still_hash_to_their_frozen_digests() -> None:
    v21 = prompt_v21_text()
    assert "sha256:" + hashlib.sha256(v21.encode("utf-8")).hexdigest() == FROZEN_V21_DIGEST
    v2 = PROMPT_V2_PATH.read_text(encoding="utf-8")
    assert "sha256:" + hashlib.sha256(v2.encode("utf-8")).hexdigest() == FROZEN_V2_DIGEST


def test_the_candidate_carries_the_three_revisions_and_nothing_forbidden() -> None:
    text = prompt_v21_text()
    for section in (
        "## EARLY PLANNING CHECKPOINT",
        "## WORKER CAPABILITY CONTRACT",
        "## IMPLEMENTATION AND VERIFICATION DISCIPLINE",
    ):
        assert section in text
    for phrase in (
        "always delegate",
        "you must delegate",
        "every task requires subagents",
        "create two workers",
    ):
        assert phrase not in text.lower()
    # The prompt is task-agnostic: it names no package, symbol or scenario of any fixture.
    for token in ("workgrid", "releaseguard", "scheduler", "dependency graph"):
        assert token not in text.lower()


# --------------------------------------------------------------------------- default identity


async def test_the_default_prompt_is_the_frozen_v1_prompt(tmp_path: Path) -> None:
    rig = PromptRig(MAIN_SYSTEM_PROMPT)
    rig.main_script = [_text("MAIN-FINAL")]

    session = await _run(tmp_path, rig)

    assert session["status"] == "succeeded"
    assert session["main_prompt"] == {
        "version": "main-v1.0",
        "digest": FROZEN_V1_DIGEST,
        "chars": len(MAIN_SYSTEM_PROMPT),
        "source": "default",
    }
    assert rig.main_system_prompts() == [MAIN_SYSTEM_PROMPT]
    assert MAIN_PROMPT_V1.digest == FROZEN_V1_DIGEST
    assert MAIN_PROMPT_V1.text == MAIN_SYSTEM_PROMPT


# --------------------------------------------------------------------------- opt-in override


async def test_the_full_candidate_reaches_the_main_verbatim_and_is_recorded(tmp_path: Path) -> None:
    frozen = prompt_v21_text()
    rig = PromptRig(frozen)
    rig.main_script = [_text("MAIN-FINAL")]

    session = await _run(
        tmp_path, rig, main_prompt=MainPromptSpec(version="main-v2.1-rc1", text=frozen)
    )

    assert rig.main_system_prompts() == [frozen]
    assert rig.main_system_prompts() != [MAIN_SYSTEM_PROMPT]
    assert session["main_prompt"] == {
        "version": "main-v2.1-rc1",
        "digest": FROZEN_V21_DIGEST,
        "chars": len(frozen),
        "source": "override",
    }
    # A prompt is not where the task lives: the goal stays in the task turn, never in the prefix.
    assert GOAL not in frozen
    assert GOAL in json.dumps(
        [item for item in rig.requests if item["role"] == "main"][0]["messages"], sort_keys=True
    )


async def test_the_override_is_a_per_run_option_not_a_global(tmp_path: Path) -> None:
    first = PromptRig(prompt_v21_text())
    first.main_script = [_text("MAIN-FINAL")]
    second = PromptRig(MAIN_SYSTEM_PROMPT)
    second.main_script = [_text("MAIN-FINAL")]

    override_session = await _run(
        tmp_path, first, main_prompt=MainPromptSpec(version="main-v2.1-rc1", text=prompt_v21_text())
    )
    default_session = await _run(tmp_path, second)

    assert override_session["main_prompt"]["source"] == "override"
    assert default_session["main_prompt"]["source"] == "default"
    # The second run is byte-identical to the historical prompt: the first run left no trace.
    assert second.main_system_prompts() == [MAIN_SYSTEM_PROMPT]
    assert default_session["main_prompt"]["digest"] == FROZEN_V1_DIGEST


async def test_an_empty_override_is_refused_before_a_run_starts() -> None:
    with pytest.raises(ValueError):
        MainPromptSpec(version="main-v2.1-rc1", text="   \n")
    with pytest.raises(ValueError):
        MainPromptSpec(version="  ", text="# ATLASRUNTIME")


# --------------------------------------------------------------------------- child isolation


async def test_the_override_never_reaches_a_delegated_child(tmp_path: Path) -> None:
    scripts = {"MARK-BETA": [_text("BETA-RESULT: arithmetic lives in app.py")]}

    def rig_with(prompt: str) -> PromptRig:
        rig = PromptRig(prompt)
        rig.child_scripts.update(scripts)
        rig.main_script = [
            _delegate_call([_task("beta", "MARK-BETA find where arithmetic is implemented")]),
            _text("MAIN-FINAL: integrated"),
        ]
        return rig

    v1_rig = rig_with(MAIN_SYSTEM_PROMPT)
    v1_session = await _run_in(tmp_path / "v1", v1_rig)
    v21_rig = rig_with(prompt_v21_text())
    v21_session = await _run_in(
        tmp_path / "v21",
        v21_rig,
        main_prompt=MainPromptSpec(version="main-v2.1-rc1", text=prompt_v21_text()),
    )

    assert v1_session["delegation"]["batches"][0]["route"] == "delegate_one"
    assert v21_session["delegation"]["batches"][0]["route"] == "delegate_one"
    # The child prompt is the trusted worker-profile render; byte-identical under both prompts, and
    # it never contains the Main's prompt.
    assert v1_rig.child_system_prompts() == v21_rig.child_system_prompts()
    child_prompt = v21_rig.child_system_prompts()[0]
    assert child_prompt.startswith("TRUSTED WORKER PROFILE")
    assert prompt_v21_text() not in child_prompt
    assert MAIN_SYSTEM_PROMPT not in child_prompt


# --------------------------------------------------------------------------- surface unchanged


async def test_the_tool_surface_is_unchanged_under_the_candidate(tmp_path: Path) -> None:
    rig = PromptRig(prompt_v21_text())
    rig.child_scripts["MARK-BETA"] = [_text("BETA")]
    rig.main_script = [_delegate_call([_task("beta", "MARK-BETA anything")]), _text("MAIN-FINAL")]

    session = await _run(
        tmp_path, rig, main_prompt=MainPromptSpec(version="main-v2.1-rc1", text=prompt_v21_text())
    )

    assert "delegate_tasks" in session["tool_surface"]
    assert "delegate.read" in session["tool_surface"]
    # The router decision is the production one and is not a function of the prompt.
    batch = session["delegation"]["batches"][0]
    assert batch["decision"]["policy_version"] == "m.1.0"
    assert batch["children"][0]["success"] is True
    # The Main consumed the handoff: the second request carries the child's summary.
    second = json.dumps(
        [item for item in rig.requests if item["role"] == "main"][1]["messages"], sort_keys=True
    )
    assert "BETA" in second
