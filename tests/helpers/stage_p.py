"""Shared fixtures for the stage-P main-driven pipeline tests.

The provider is scripted; everything else (engine, AgentRunner, tool runtime, action gate, ledger,
ownership policy, sandbox lifecycle, handoff store, oracle) is the production code path.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from pydantic import BaseModel, Field

from atlasos.app.goal_sandbox import GoalSandboxOptions
from atlasos.app.goal_verification import GoalAcceptance
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

Response = Callable[[list[LLMMessage]], list[Any]]

#: A small project with two independent modules. ``subtract`` is missing, so the public suite fails
#: until the Main integrates the change.
PROJECT_FILES: dict[str, str] = {
    "README.md": (
        "# Arithmetic project\n\n"
        "`app/calc.py` provides arithmetic helpers. `app/text.py` provides text helpers.\n"
        "The public contract requires `add` and `subtract` in `app/calc.py`.\n"
    ),
    "app/__init__.py": "",
    "app/calc.py": ('def add(a, b):\n    """Return the sum of two numbers."""\n    return a + b\n'),
    "app/text.py": (
        'def normalize(value):\n    """Collapse whitespace."""\n    return value.strip()\n'
    ),
    "tests/test_public.py": (
        "from app.calc import add, subtract\n"
        "from app.text import normalize\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "\n"
        "\n"
        "def test_subtract():\n"
        "    assert subtract(9, 4) == 5\n"
        "\n"
        "\n"
        "def test_normalize():\n"
        "    assert normalize('  x  ') == 'x'\n"
    ),
}


def git_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """Create a committed git repository; provisioning refuses a dirty worktree."""

    root.mkdir(parents=True, exist_ok=True)
    for name, text in (files or PROJECT_FILES).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    commands = (
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=stage-p@example.invalid",
            "-c",
            "user.name=Stage P",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
    )
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True)
    return root


class PipelineRig:
    """One scripted provider for the Main and every child, keyed by system prompt."""

    def __init__(self) -> None:
        self.main_script: list[Response] = []
        self.child_scripts: dict[str, list[Response]] = {}
        self.requests: list[dict[str, Any]] = []
        self.calls: dict[str, int] = {}
        self.barrier: asyncio.Barrier | None = None
        self.child_started: dict[str, asyncio.Event] = {}
        self.fail_children: dict[str, str] = {}
        self.slow_children: dict[str, float] = {}
        self.usage: dict[str, tuple[int, int, int]] = {
            "main": (100, 40, 10),
            "calc": (200, 50, 20),
            "text": (300, 60, 30),
        }
        self.name = "stage-p-scripted"

    def build(self) -> PipelineRig:
        return self

    def _role(self, system_prompt: str | None) -> str:
        text = system_prompt or ""
        if text.startswith("ATLAS MAIN AGENT"):
            return "main"
        for marker in self.child_scripts:
            if marker in text:
                return marker
        return "unknown"

    def _next(self, role: str, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
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
        key = role.removeprefix("MARK-").lower()
        input_tokens, cache_read, output_tokens = self.usage.get(key, (1, 0, 1))
        has_tool_use = any(isinstance(block, ToolUseBlock) for block in blocks)
        return LLMResponse(
            id=f"{role}-{turn}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if has_tool_use else "end_turn",
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_input_tokens=cache_read,
            ),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        role = self._role(options.system_prompt)
        started = self.child_started.get(role)
        if started is not None:
            started.set()
        if role in self.fail_children:
            raise RuntimeError(self.fail_children[role])
        delay = self.slow_children.get(role)
        if delay:
            await asyncio.sleep(delay)
        if self.barrier is not None and role != "main" and self.calls.get(role, 0) == 0:
            await asyncio.wait_for(self.barrier.wait(), timeout=5)
        return self._next(role, messages, options)

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

    # -- helpers -------------------------------------------------------------

    def main_requests(self) -> list[dict[str, Any]]:
        return [item for item in self.requests if item["role"] == "main"]


def delegate_call(tasks: list[dict[str, Any]], call_id: str = "call-delegate") -> Response:
    return lambda _: [ToolUseBlock(id=call_id, name="delegate_tasks", input={"tasks": tasks})]


def tool_call(name: str, payload: dict[str, Any], call_id: str) -> Response:
    return lambda _: [ToolUseBlock(id=call_id, name=name, input=payload)]


def tool_calls(*calls: tuple[str, dict[str, Any]]) -> Response:
    """Several tool blocks in one assistant turn (AnyCode runs them concurrently)."""

    def respond(_: list[LLMMessage]) -> list[Any]:
        return [
            ToolUseBlock(id=f"call-{index}-{name}", name=name, input=payload)
            for index, (name, payload) in enumerate(calls)
        ]

    return respond


def text(value: str) -> Response:
    return lambda _: [TextBlock(text=value)]


def task(
    key: str,
    objective: str,
    *,
    background: str = "The workspace is a small Python project with app/ and tests/.",
    profile_id: str = "research_worker",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "key": key,
        "objective": objective,
        "background": background,
        "profile_id": profile_id,
        **extra,
    }


def config(**kwargs: Any) -> AtlasConfig:
    return AtlasConfig(tools=ToolsConfig(mode="progressive"), **kwargs)


def sandbox_options(source_repo: Path, factory: Callable[[Path], Any], **kwargs: Any):
    return GoalSandboxOptions(
        provider="fake",
        source_repo=source_repo,
        require_real=False,
        provider_factory=factory,
        **kwargs,
    )


class ContainerSpy(BaseModel):
    """Stands in for the sandbox provider factory so a test can inspect the provider object."""

    created: list[Any] = Field(default_factory=list)


def recording_factory(spy: ContainerSpy, factory: Callable[[Path], Any]):
    def build(root: Path) -> Any:
        provider = factory(root)
        spy.created.append(provider)
        return provider

    return build


def acceptance() -> GoalAcceptance:
    """The Host contract for the fixture project: ``subtract`` exists and the suite passes."""

    spec = VerificationSpec(
        spec_id="stage-p-calculator",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=("app/calc.py",),
        checks=(
            CheckSpec(
                check_id="calc-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="app/calc.py",
            ),
            CheckSpec(
                check_id="subtract-defined",
                category="PUBLIC_CONTRACT",
                kind=CheckKind.FUNCTION_PRESENT,
                artifact="app/calc.py",
                params={"functions": ["subtract"]},
            ),
            CheckSpec(
                check_id="calc-parses",
                category="STRUCTURAL",
                kind=CheckKind.PYTHON_PARSES,
                artifact="app/calc.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(spec=spec, dynamic_command="python3 -m pytest -q")


def session_json(session: dict[str, Any]) -> dict[str, Any]:
    return json.loads((Path(session["run_dir"]) / "session.json").read_text(encoding="utf-8"))
