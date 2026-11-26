"""Stage F3 failure observability: a provider exception has to reach the session.

Every test drives the real ``run_goal`` and the real AnyCode agent loop with a deterministic
adapter; nothing here calls a model or opens a sandbox (``sandbox=None`` is the host path). The
defect being closed: ``AgentRunner.run`` returns an empty fallback result when the adapter stream
ends with an ``error`` event, so the provider failure reached the session as "success, no messages,
no usage" and the cause was lost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from anycode.types import (
    LLMChatOptions,
    LLMResponse,
    LLMStreamOptions,
    StreamEvent,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)

from atlasos.app.main_driven import GoalRunOptions, run_goal
from atlasos.config import AtlasConfig, ToolsConfig


class ProviderError(RuntimeError):
    """The shape a provider SDK error has: a message, an HTTP status and a provider code."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code


class FaultAdapter:
    """A provider whose turns are scripted: text, a tool call, or a failure.

    ``script`` is consumed one entry per request. An entry is either ``"text"`` (a normal end),
    ``"tool"`` (one tool call, so the loop continues) or an exception instance (raised before any
    response is produced). ``partial`` makes the failing stream emit one text event first.
    """

    name = "stage-f-fault"

    def __init__(self, script: list[Any], *, partial: bool = False) -> None:
        self.script = list(script)
        self.partial = partial
        self.calls = 0
        self.index = 0
        self.requests: list[int] = []

    def build(self) -> FaultAdapter:
        return self

    def _entry(self) -> Any:
        """The current turn's entry. It is sticky: a failing turn fails every attempt, including
        the runner's ``stream`` -> ``chat`` fallback, which is what the real path does."""

        if self.index >= len(self.script):
            return "text"
        return self.script[self.index]

    def _succeeded(self) -> None:
        self.index += 1

    def _next(self, options: Any) -> Any:
        self.calls += 1
        self.requests.append(self.index)
        return self._entry()

    def _response(self, options: Any, blocks: list[Any], stop_reason: str) -> LLMResponse:
        return LLMResponse(
            id=f"fault-{self.calls}",
            content=blocks,
            model=options.model,
            stop_reason=stop_reason,
            usage=TokenUsage(input_tokens=120, output_tokens=30),
        )

    async def chat(self, messages: list[Any], options: LLMChatOptions) -> LLMResponse:
        entry = self._next(options)
        if isinstance(entry, BaseException):
            raise entry
        if entry == "tool":
            self._succeeded()
            return self._response(
                options, [ToolUseBlock(id="call-1", name="list_files", input={})], "tool_use"
            )
        self._succeeded()
        return self._response(options, [TextBlock(text="arithmetic lives in app.py")], "end_turn")

    def stream(self, messages: list[Any], options: LLMStreamOptions):
        async def _generate():
            entry = self._next(options)
            if isinstance(entry, BaseException):
                if self.partial:
                    yield StreamEvent(type="text", data="partial output")
                raise entry
            if entry == "tool":
                block = ToolUseBlock(id="call-1", name="list_files", input={})
                yield StreamEvent(type="tool_use", data=block)
                self._succeeded()
                yield StreamEvent(type="done", data=self._response(options, [block], "tool_use"))
                return
            block = TextBlock(text="arithmetic lives in app.py")
            yield StreamEvent(type="text", data=block.text)
            self._succeeded()
            yield StreamEvent(type="done", data=self._response(options, [block], "end_turn"))

        return _generate()


def _config() -> AtlasConfig:
    return AtlasConfig(tools=ToolsConfig(mode="progressive"))


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return workspace


async def _run(tmp_path: Path, adapter: FaultAdapter, *, run_id: str) -> dict[str, Any]:
    options = GoalRunOptions(
        goal="Add a subtract function and report what arithmetic exists.",
        runs_root=tmp_path / "runs",
        workspace_root=_workspace(tmp_path),
        config=_config(),
        provider_factory=adapter.build,
        run_id=run_id,
    )
    return await run_goal(options)


def _records(session: dict[str, Any]) -> list[dict[str, Any]]:
    return list((session.get("model_requests") or {}).get("records") or [])


# -- 1/3/6: the first request fails before any response ------------------------------------------


async def test_the_first_request_failure_is_persisted_and_stops_as_infrastructure(
    tmp_path: Path,
) -> None:
    adapter = FaultAdapter([ProviderError("upstream refused the request", status_code=400)])
    session = await _run(tmp_path, adapter, run_id="f3-fault-first")

    failure = session["failure"]
    assert failure["kind"] == "provider_error"
    assert "upstream refused the request" in failure["message"]
    assert failure["provider_error"]["exception_type"] == "ProviderError"
    assert failure["provider_error"]["http_status"] == 400
    assert failure["provider_error"]["usage_reported"] is False

    # The runner tries ``stream`` and then falls back to one ``chat`` call when nothing was
    # emitted, so the same failing turn is recorded twice — two attempts, two terminal states.
    records = _records(session)
    assert records and {record["status"] for record in records} == {"FAILED"}
    assert records[0]["request_id"]
    assert records[0]["started_at"] and records[0]["finished_at"]
    assert records[0]["usage"] is None
    counts = (session.get("model_requests") or {}).get("counts") or {}
    assert counts.get("FAILED") == len(records)
    assert counts.get("COMPLETED") == 0

    # The raw AgentResult still says success (that is AnyCode's empty run); the runtime does not.
    assert session["main"]["success"] is True
    assert session["execution_succeeded"] is False
    assert session["status"] == "failed"

    # Usage stays unknown: no number is invented for the failed request.
    assert session["metrics"]["main_cumulative_input_tokens"] is None


async def test_the_request_journal_is_written_next_to_the_session(tmp_path: Path) -> None:
    adapter = FaultAdapter([ProviderError("connection reset by peer")])
    session = await _run(tmp_path, adapter, run_id="f3-fault-journal")

    journal = Path(session["run_dir"]) / "provider_requests.json"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert payload["run_id"] == "f3-fault-journal"
    assert payload["counts"]["FAILED"] >= 1
    assert payload["counts"]["COMPLETED"] == 0
    assert payload["usage_observed"] is False
    assert payload["records"][0]["message"].startswith("connection reset")


# -- 2: the stream fails after partial output ---------------------------------------------------


async def test_a_stream_that_fails_mid_turn_records_a_partial_response(tmp_path: Path) -> None:
    adapter = FaultAdapter([ProviderError("stream interrupted", status_code=503)], partial=True)
    session = await _run(tmp_path, adapter, run_id="f3-fault-partial")

    record = _records(session)[0]
    assert record["status"] == "FAILED"
    assert record["partial_response"] is True
    assert record["http_status"] == 503
    assert session["failure"]["kind"] == "provider_error"


# -- 4: a later turn fails after a completed turn with real usage -------------------------------


async def test_a_failure_after_a_completed_turn_keeps_the_real_usage(tmp_path: Path) -> None:
    adapter = FaultAdapter(["tool", ProviderError("second turn failed", status_code=500)])
    session = await _run(tmp_path, adapter, run_id="f3-fault-second")

    counts = (session.get("model_requests") or {}).get("counts") or {}
    assert counts.get("COMPLETED") == 1
    assert counts.get("FAILED") >= 1
    completed = next(record for record in _records(session) if record["status"] == "COMPLETED")
    assert completed["usage_reported"] is True
    assert completed["usage"]["input_tokens"] == 120

    # The completed turn's provider-reported usage survives; the failed turn adds nothing.
    assert session["metrics"]["main_cumulative_input_tokens"] == 120
    assert session["failure"]["kind"] == "provider_error"
    assert session["execution_succeeded"] is False


# -- 5: a normal request and completion are unaffected ------------------------------------------


async def test_a_normal_run_completes_and_records_one_completed_request(tmp_path: Path) -> None:
    adapter = FaultAdapter(["text"])
    session = await _run(tmp_path, adapter, run_id="f3-fault-normal")

    assert session["status"] == "succeeded"
    assert session["failure"] is None
    assert session["execution_succeeded"] is True
    counts = (session.get("model_requests") or {}).get("counts") or {}
    assert counts.get("COMPLETED") == 1
    assert counts.get("FAILED") == 0
    record = _records(session)[0]
    assert record["status"] == "COMPLETED"
    assert record["usage_reported"] is True
    assert record["returned_model"] == record["requested_model"]
    assert session["metrics"]["main_cumulative_input_tokens"] == 120


# -- the chain: session -> Unified Cell Evidence -> executor classification ---------------------


async def test_the_journaled_failure_reaches_the_executor_and_the_usage_stop_still_fires(
    tmp_path: Path,
) -> None:
    from experiments.stage_b_plus import cost, executor
    from experiments.stage_f import normalize, plan

    cell = next(cell for cell in plan.batch_cells(1) if cell["arm_id"] == "A1")
    adapter = FaultAdapter([ProviderError("upstream refused the request", status_code=400)])
    session = await _run(tmp_path, adapter, run_id="f3-fault-audit")

    evidence = normalize.from_main_child_session(
        cell,
        session,
        oracle={"status": "FAIL", "passed": False},
        export={"workspace_path": None},
    )
    # The raw empty AgentResult is not counted as completion.
    assert evidence["correctness"]["raw_agent_success"] is True
    assert evidence["correctness"]["agent_success"] is False
    assert evidence["correctness"]["failure_kind"] == "provider_error"

    outcome = executor.audit_cell(
        cell=cell,
        run=executor.CellRun(
            session=evidence, run_dir=None, fixture_root=None, oracle=None, duration_s=0.1
        ),
        plan={"plan_id": cell["plan_id"], "scope": cell["scope"]},
        usage_before=None,
        cost_before=None,
        prices=dict(cost.stage_b_estimate.pricing()),
    )

    assert outcome["verdict"] == "STOPPED"
    assert outcome["failure_kind"] == "provider_error"
    assert executor.STOP_UNKNOWN_USAGE in outcome["stops"]
    assert outcome["cost_usd"] is None
    assert outcome["usage"]["main_cumulative_input_tokens"] is None
