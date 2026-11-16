"""Deterministic scripted provider for the Closure-1B acceptance. No model is called.

The script replays the calls a competent worker would make — write its owned files, read one back,
list the directory, grep, run a command — through the *real* ``ToolRegistry``/``ToolExecutor`` into
the *real* container. The solution bodies come from the frozen Final-4 task definition
(``validation_tasks.TASKS[i].solution``), so the hidden oracle can pass on merit rather than on a
special case.
"""

from __future__ import annotations

from typing import Any

from anycode.plugins.registry import register_provider_factory
from anycode.providers.fake import FakeAdapter, FakeResponse
from experiments.evaluation.final4.validation_tasks import Task


class ScriptedAdapter(FakeAdapter):
    """Replays ``(tool_name, input)`` pairs, then ends the loop with a text answer."""

    def __init__(
        self,
        calls: list[tuple[str, dict[str, Any]]],
        final_text: str,
    ) -> None:
        responses = [FakeResponse(tool_calls=((name, payload),)) for name, payload in calls]
        responses.append(FakeResponse(text=final_text))
        super().__init__(responses=responses)


class AssignmentAwareAdapter(FakeAdapter):
    """One provider serving every worker of a multi arm, one script per worker.

    Every agent shares the provider name, so the adapter picks the script from the *task turn it was
    actually given*: the longest registered marker present in the first user message wins. The
    shared-context header is a prefix of every turn, the worker header is not, so the worker's own
    script is chosen.
    """

    def __init__(
        self,
        scripts: dict[str, list[tuple[str, dict[str, Any]]]],
        *,
        final_text: str = "implemented and checked",
    ) -> None:
        super().__init__(responses=[])
        self._scripts = scripts
        self._order = sorted(scripts, key=len, reverse=True)
        self._cursors = dict.fromkeys(scripts, 0)
        self._final_text = final_text

    def _step_for(self, messages: list[Any]) -> str:
        first = messages[0] if messages else None
        blocks = getattr(first, "content", ()) or ()
        text = "\n".join(str(getattr(block, "text", "")) for block in blocks)
        for marker in self._order:
            if marker and marker in text:
                return marker
        raise KeyError("no scripted assignment marker matched the task turn")

    async def chat(self, messages: list[Any], options: Any) -> Any:  # type: ignore[override]
        marker = self._step_for(messages)
        calls = self._scripts[marker]
        cursor = self._cursors[marker]
        if cursor < len(calls):
            name, payload = calls[cursor]
            self.responses = [FakeResponse(tool_calls=((name, payload),))]
            self._cursors[marker] = cursor + 1
        else:
            self.responses = [FakeResponse(text=self._final_text)]
        self._cursor = 0
        return await super().chat(messages, options)


def solution_calls(task: Task, step_id: str | None) -> list[tuple[str, dict[str, Any]]]:
    """The files this agent writes, followed by one read/list/grep/bash round trip."""

    if step_id is None:
        files = {path: task.solution[path] for path in sorted(task.solution)}
    else:
        worker = next(item for item in task.workers if str(item["task_id"]) == step_id)
        owned = [str(path) for path in worker["owned_paths"]]  # type: ignore[union-attr]
        files = {path: task.solution[path] for path in owned}
    if not files:
        raise ValueError(f"{task.task_id}/{step_id}: no owned files to write")
    first = sorted(files)[0]
    return [
        *(("file_write", {"path": path, "content": content}) for path, content in files.items()),
        ("file_read", {"path": first}),
        ("list_files", {"path": "."}),
        ("grep", {"pattern": "def ", "path": "."}),
        ("bash", {"command": "python3 -m compileall -q . && echo COMPILE_OK"}),
    ]


def register_scripted_provider(
    name: str,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    final_text: str = "implemented and checked",
) -> None:
    async def factory(**_: Any) -> ScriptedAdapter:
        return ScriptedAdapter(calls, final_text)

    register_provider_factory(name, factory)


def register_assignment_aware_provider(
    name: str,
    scripts: dict[str, list[tuple[str, dict[str, Any]]]],
    *,
    final_text: str = "implemented and checked",
) -> None:
    async def factory(**_: Any) -> AssignmentAwareAdapter:
        return AssignmentAwareAdapter(scripts, final_text=final_text)

    register_provider_factory(name, factory)


def assignment_marker(worker: dict[str, Any]) -> str:
    """The first line of a worker's assignment: unique per worker, stable across runs."""

    return str(worker["assignment"]).splitlines()[0]


def failure_calls(tool_name: str, payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """One call that is expected to come back as a tool error."""

    return [(tool_name, payload)]
