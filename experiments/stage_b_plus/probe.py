"""Phase B: does the Main-driven path actually support a coding worker?

A scripted proposal is not evidence of autonomous delegation, and this module never counts as one.
It answers a narrower question that has to be answered *before* spending money: when a legal
``delegate_tasks`` call names ``coding_worker``, does the real runtime resolve the profile, admit
the plan, build a writable child, enforce the write scope, hand the result back and let the Main
continue?

Everything except the model is production code: the real ``run_goal``, the real router, the real
ownership policy, the real handoff store. Only the provider is scripted, and the script is chosen by
the trusted system prompt the runtime itself composed.

Five probes, each a separate run:

``coding_parallel``      two coding workers, disjoint declared paths; admission and writes expected.
``research_write``       a research worker that declares a write — expected router refusal.
``shared_write``         two coding workers declaring one path — expected shared-write refusal.
``single_coding``        one coding task — expected trivial-task refusal.
``research_tool_escape`` a research worker that emits ``file_write`` although the profile never
                         offers it — expected ownership denial, which is the real enforcement.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
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
from experiments.stage_b_plus import contract

from atlasos.app.main_driven import GoalRunOptions, MainPromptSpec, run_goal
from atlasos.config import AtlasConfig, ToolsConfig
from atlasos.delegation.config import DelegationConfig

Response = Callable[[list[LLMMessage]], list[Any]]

WORKSPACE_FILES: dict[str, str] = {
    "package/__init__.py": "",
    "package/a.py": 'def alpha():\n    """Placeholder."""\n    return None\n',
    "package/b.py": 'def beta():\n    """Placeholder."""\n    return None\n',
    "package/integration.py": '"""Integration surface the Main owns."""\n',
    "README.md": "# probe project\n\nTwo independent modules and one integration surface.\n",
}

MARK_ALPHA = "MARK-ALPHA"
MARK_BETA = "MARK-BETA"
MARK_GAMMA = "MARK-GAMMA"

_REF_RE = re.compile(r"ho-[0-9a-f]{24}")


# -- scripted provider ------------------------------------------------------------------------


@dataclass
class ScriptedProvider:
    """Serves the Main and every child from one script table, keyed by the system prompt."""

    main_script: list[Response]
    child_scripts: dict[str, list[Response]]
    name: str = "bplus-probe"
    requests: list[dict[str, Any]] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)

    def _role(self, system_prompt: str | None) -> str:
        text = system_prompt or ""
        if "TRUSTED WORKER PROFILE" not in text:
            return "main"
        for marker in self.child_scripts:
            if marker in text:
                return marker
        return "unknown-child"

    def _respond(self, role: str, messages: list[LLMMessage], options: Any) -> LLMResponse:
        self.requests.append(
            {
                "role": role,
                "system_prompt": options.system_prompt,
                "tools": [tool.name for tool in (options.tools or ())],
            }
        )
        turn = self.calls.get(role, 0)
        self.calls[role] = turn + 1
        script = self.main_script if role == "main" else self.child_scripts.get(role, [])
        blocks: list[Any] = (
            script[turn](messages) if turn < len(script) else [TextBlock(text="done")]
        )
        has_tool = any(isinstance(block, ToolUseBlock) for block in blocks)
        return LLMResponse(
            id=f"{role}-{turn}",
            content=blocks,
            model=options.model,
            stop_reason="tool_use" if has_tool else "end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
        )

    async def chat(self, messages: list[LLMMessage], options: LLMChatOptions) -> LLMResponse:
        return self._respond(self._role(options.system_prompt), messages, options)

    def stream(self, messages: list[LLMMessage], options: LLMStreamOptions):
        response = self._respond(self._role(options.system_prompt), messages, options)

        async def _gen():
            for block in response.content:
                if isinstance(block, ToolUseBlock):
                    yield StreamEvent(type="tool_use", data=block)
                else:
                    yield StreamEvent(type="text", data=getattr(block, "text", ""))
            yield StreamEvent(type="done", data=response)

        return _gen()

    def tool_surface_of(self, role: str) -> list[str]:
        for request in self.requests:
            if request["role"] == role:
                return list(request["tools"])
        return []


# -- script helpers ---------------------------------------------------------------------------


def call(name: str, payload: dict[str, Any], call_id: str) -> Response:
    return lambda _messages: [ToolUseBlock(id=call_id, name=name, input=payload)]


def say(text: str) -> Response:
    return lambda _messages: [TextBlock(text=text)]


def _text_of(messages: Sequence[Any]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in getattr(message, "content", ()) or ():
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            content = getattr(block, "content", None)
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, (list, tuple)):
                parts.extend(str(item) for item in content)
    return "\n".join(parts)


def read_first_ref(_messages: list[LLMMessage]) -> list[Any]:
    """Read the first handoff reference the conversation has seen, exactly once."""

    match = _REF_RE.search(_text_of(_messages))
    if match is None:
        return [TextBlock(text="no handoff reference was returned")]
    return [
        ToolUseBlock(
            id="call-read-1", name="delegate_read", input={"ref_id": match.group(0), "offset": 0}
        )
    ]


def task(
    key: str,
    objective: str,
    *,
    profile_id: str,
    write_paths: Sequence[str] = (),
    background: str = "The workspace is a small Python project under package/.",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "key": key,
        "objective": objective,
        "background": background,
        "profile_id": profile_id,
    }
    if write_paths:
        payload["write_paths"] = list(write_paths)
    return payload


# -- the run ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    probe_id: str
    profile_id: str
    tasks: tuple[dict[str, Any], ...]
    main_script: tuple[Response, ...]
    child_scripts: Mapping[str, tuple[Response, ...]]
    main_write_paths: tuple[str, ...] = ()
    worker_exposure: str = "card"


@dataclass
class ProbeResult:
    probe_id: str
    checks: list[dict[str, Any]]
    session: dict[str, Any]
    provider: dict[str, Any]
    workspace: dict[str, Any]
    conversations: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return all(check["ok"] for check in self.checks)


def _write_workspace(root: Path, files: Mapping[str, str]) -> None:
    for name, text in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _options(probe: Probe, *, root: Path, provider: ScriptedProvider) -> GoalRunOptions:
    workspace = root / "workspace"
    _write_workspace(workspace, WORKSPACE_FILES)
    return GoalRunOptions(
        goal="Implement the two placeholder modules and integrate them.",
        runs_root=root / "runs",
        workspace_root=workspace,
        config=AtlasConfig(tools=ToolsConfig(mode="progressive")),
        delegation=DelegationConfig(
            enabled=True,
            max_parallel_children=2,
            max_children_per_call=2,
            max_delegation_rounds=3,
            child_timeout_seconds=120.0,
            child_max_turns=int(contract.BUDGET["child_max_turns"]),
            result_preview_chars=900,
        ),
        provider="fake",
        model="deepseek-chat",
        max_turns=8,
        main_write_paths=probe.main_write_paths,
        child_write_grants={probe.profile_id: ("package/",)} if probe.profile_id ==
        contract.CODING else {},
        worker_profiles=(probe.profile_id,),
        worker_max_turns=int(contract.BUDGET["worker_max_turns"]),
        worker_exposure=probe.worker_exposure,
        provider_factory=lambda: provider,
        # Unique per invocation: the provider registry is process-global and refuses a duplicate
        # name, and a probe may legitimately be re-run inside one process.
        run_id=f"bplus-probe-{probe.probe_id}-{uuid.uuid4().hex[:8]}",
        main_prompt=MainPromptSpec(
            version=contract.prompt_version("p3-bplus"),
            text=contract.prompt_text("p3-bplus"),
        ),
    )


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _ownership_denials(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(((session.get("ownership") or {}).get("denials")) or [])


def _denied_reasons(session: Mapping[str, Any]) -> list[str]:
    return [str(item.get("reason_code")) for item in _ownership_denials(session)]


def _child_calls(run_dir: Path, worker_ids: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Every tool call a child made, with the text of its result.

    The conversation store is the only place a refused call is still visible: a denial that never
    reached the tool body leaves no workspace trace.
    """

    path = run_dir / "tool_conversations.json"
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = (payload.get("tasks") or {}) if isinstance(payload, dict) else {}
    collected: dict[str, list[dict[str, Any]]] = {}
    for worker_id in worker_ids:
        messages = (tasks.get(worker_id) or {}).get("messages") or []
        results: dict[str, str] = {}
        for message in messages:
            for block in message.get("content") or ():
                if isinstance(block, dict) and block.get("tool_use_id"):
                    results[str(block["tool_use_id"])] = str(block.get("content"))[:400]
        calls: list[dict[str, Any]] = []
        for message in messages:
            for block in message.get("content") or ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append(
                        {
                            "tool": block.get("name"),
                            "input": block.get("input"),
                            "result": results.get(str(block.get("id"))),
                        }
                    )
        collected[worker_id] = calls
    return collected


def run_probe(probe: Probe, *, root: Path | None = None) -> ProbeResult:
    """Run one probe. ``root`` keeps the run directory for later analysis; ``None`` discards it."""

    provider = ScriptedProvider(
        main_script=list(probe.main_script),
        child_scripts={key: list(value) for key, value in probe.child_scripts.items()},
    )
    with contextlib.ExitStack() as stack:
        if root is None:
            temporary = stack.enter_context(
                tempfile.TemporaryDirectory(prefix=f"bplus-probe-{probe.probe_id}-")
            )
            base = Path(temporary)
        else:
            base = Path(root) / probe.probe_id
            base.mkdir(parents=True, exist_ok=True)
        options = _options(probe, root=base, provider=provider)
        session = asyncio.run(run_goal(options))
        workspace = options.workspace_root
        files = {
            name: (workspace / name).read_text(encoding="utf-8")
            for name in sorted(WORKSPACE_FILES)
            if (workspace / name).is_file()
        }
        extra = sorted(
            str(path.relative_to(workspace)).replace("\\", "/")
            for path in workspace.rglob("*")
            if path.is_file()
            and str(path.relative_to(workspace)).replace("\\", "/") not in WORKSPACE_FILES
        )
        worked = [
            str(child.get("worker_id")) for child in _children(session) if child.get("worker_id")
        ]
        conversations = _child_calls(Path(session["run_dir"]), worked)
    checks = _evaluate(probe, session, files, extra, provider, conversations)
    return ProbeResult(
        probe_id=probe.probe_id,
        checks=checks,
        session=session,
        provider={
            "roles": sorted(provider.calls),
            "requests": len(provider.requests),
            "main_tool_surface": provider.tool_surface_of("main"),
            "child_tool_surface": {
                marker: provider.tool_surface_of(marker) for marker in probe.child_scripts
            },
        },
        workspace={"files": sorted(files), "extra_files": extra},
        conversations=dict(conversations),
    )


def _batches(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(((session.get("delegation") or {}).get("batches")) or [])


def _children(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [child for batch in _batches(session) for child in (batch.get("children") or [])]


def batch_reasons(batch: Mapping[str, Any] | None) -> list[str]:
    """Reason codes live on the decision, not on the batch."""

    if batch is None:
        return []
    decision = batch.get("decision") or {}
    return [str(item) for item in (decision.get("reason_codes") or ())]


def _evaluate(
    probe: Probe,
    session: Mapping[str, Any],
    files: Mapping[str, str],
    extra: Sequence[str],
    provider: ScriptedProvider,
    conversations: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    batches = _batches(session)
    route = batches[0].get("route") if batches else None
    reasons = batch_reasons(batches[0] if batches else None)
    children = _children(session)
    main_tools = provider.tool_surface_of("main")

    checks.append(
        _check("main_has_delegate_tasks", "delegate_tasks" in main_tools, f"surface={main_tools}")
    )

    if probe.probe_id == "coding_parallel":
        checks.append(_check("route_is_parallel", route == "delegate_parallel", f"route={route}"))
        checks.append(
            _check("two_children_admitted", len(children) == 2, f"children={len(children)}")
        )
        checks.append(
            _check(
                "every_child_completed",
                bool(children) and all(child.get("success") for child in children),
                json.dumps(
                    [(child.get("key"), child.get("status")) for child in children]
                ),
            )
        )
        checks.append(
            _check(
                "granted_write_landed",
                "def alpha" in files.get("package/a.py", "")
                and "def beta" in files.get("package/b.py", ""),
                f"a={files.get('package/a.py')!r} b={files.get('package/b.py')!r}",
            )
        )
        checks.append(
            _check(
                "outside_write_blocked",
                "package/forbidden.py" not in extra,
                f"extra={list(extra)}",
            )
        )
        denials = _denied_reasons(session)
        checks.append(
            _check(
                "ownership_denied_the_escape",
                "OWNERSHIP_DENIED_UNDECLARED_PATH" in denials,
                f"denials={denials}",
            )
        )
        checks.append(
            _check(
                "interpreter_shell_blocked",
                "OWNERSHIP_UNCLASSIFIABLE_SHELL" in denials,
                f"denials={denials}",
            )
        )
        checks.append(
            _check(
                "main_continued_after_handoff",
                "package/integration.py" in files
                and "integrated two modules" in files.get("package/integration.py", ""),
                f"integration={files.get('package/integration.py')!r}",
            )
        )
        reads = (session.get("handoff_reads") or {}).get("read_success")
        checks.append(_check("handoff_read_succeeded", bool(reads), f"read_success={reads}"))
        child_surface = provider.tool_surface_of(MARK_ALPHA)
        checks.append(
            _check(
                "coding_child_offered_write_tools",
                {"file_write", "file_edit", "bash"} <= set(child_surface),
                f"surface={child_surface}",
            )
        )
        checks.append(
            _check(
                "coding_child_not_offered_host_tools",
                "run_public_tests" not in child_surface
                and "delegate_tasks" not in child_surface,
                f"surface={child_surface}",
            )
        )

    elif probe.probe_id == "research_write":
        checks.append(
            _check(
                "router_refused_declared_write",
                route == "direct" and "WRITE_SCOPE_NOT_GRANTED" in reasons,
                f"route={route} reasons={reasons}",
            )
        )
        checks.append(_check("no_worker_started", not children, f"children={len(children)}"))
        checks.append(
            _check(
                "workspace_untouched_by_worker",
                "PROBE-ALPHA" not in files.get("package/a.py", ""),
                "package/a.py unchanged",
            )
        )

    elif probe.probe_id == "shared_write":
        checks.append(
            _check(
                "router_refused_shared_path",
                route == "direct" and "SHARED_WRITE_RISK" in reasons,
                f"route={route} reasons={reasons}",
            )
        )
        checks.append(_check("no_worker_started", not children, f"children={len(children)}"))

    elif probe.probe_id == "single_coding":
        checks.append(
            _check(
                "router_refused_single_write_task",
                route == "direct" and "TRIVIAL_DIRECT_TASK" in reasons,
                f"route={route} reasons={reasons}",
            )
        )
        checks.append(_check("no_worker_started", not children, f"children={len(children)}"))

    elif probe.probe_id == "research_tool_escape":
        escape_surface = provider.tool_surface_of(MARK_GAMMA)
        checks.append(
            _check(
                "research_profile_offers_no_write_tool",
                "file_write" not in escape_surface,
                f"surface={escape_surface}",
            )
        )
        checks.append(
            _check(
                "escape_attempt_did_not_write",
                "package/escaped.py" not in extra,
                f"extra={list(extra)}",
            )
        )
        denials = _denied_reasons(session)
        # Which layer refused matters: a runner that drops an unoffered tool is a different defence
        # from an ownership policy that denies a write. Record the channel that actually fired.
        escape_calls = [
            call
            for calls in conversations.values()
            for call in calls
            if call.get("tool") == "file_write"
            and str((call.get("input") or {}).get("path")) == "package/escaped.py"
        ]
        channel = "executed"
        if "OWNERSHIP_DENIED_UNDECLARED_PATH" in denials or "OWNERSHIP_UNKNOWN_WORKER" in denials:
            channel = "ownership"
        elif escape_calls and escape_calls[0].get("result"):
            channel = "runner"
        checks.append(
            _check(
                "escape_attempt_refused_before_write",
                channel in {"ownership", "runner"},
                f"channel={channel} denials={denials} result={escape_calls[:1]}",
            )
        )
    else:
        # A caller may define its own probe (the offline checks add one). No expectation is attached
        # to an unknown id, so only the observable outcome is recorded.
        checks.append(
            _check(
                "probe_outcome_recorded",
                True,
                f"route={route} reasons={reasons} children={len(children)}",
            )
        )

    return checks


# -- the probe table --------------------------------------------------------------------------


def contract_marker(name: str) -> str:
    return (
        f'def {name}():\n'
        f'    """Implemented by a scripted child for the B+ probe."""\n'
        f"    return {name!r}\n"
    )


def _delegate(calls: Sequence[dict[str, Any]], *, reason: str = "probe") -> Response:
    payload = [dict(item) for item in calls]
    return lambda _messages: [
        ToolUseBlock(id="call-delegate-1", name="delegate_tasks", input={"tasks": payload,
                                                                       "reason": reason})
    ]


def probe_table() -> tuple[Probe, ...]:
    alpha_writes: tuple[tuple[str, dict[str, Any]], ...] = (
        ("file_write", {"path": "package/a.py", "content": contract_marker("alpha")}),
        ("file_write", {"path": "package/forbidden.py", "content": "x = 1\n"}),
        ("bash", {"command": "python3 -m pytest -q"}),
        # A read-only command from the classifier's allowlist, portable to the host shell the
        # offline probe runs in. `ls` is not, and would fail for a reason unrelated to the probe.
        ("bash", {"command": "echo probe"}),
    )
    coding_pair = (
        task(
            "alpha",
            f"{MARK_ALPHA}: implement the alpha module",
            profile_id=contract.CODING,
            write_paths=("package/a.py",),
        ),
        task(
            "beta",
            f"{MARK_BETA}: implement the beta module",
            profile_id=contract.CODING,
            write_paths=("package/b.py",),
        ),
    )
    research_escape_pair = (
        task(
            "alpha",
            f"{MARK_GAMMA}: read the alpha module and report",
            profile_id=contract.RESEARCH,
        ),
        task(
            "beta",
            f"{MARK_BETA}: read the beta module and report",
            profile_id=contract.RESEARCH,
        ),
    )
    return (
        Probe(
            probe_id="coding_parallel",
            profile_id=contract.CODING,
            tasks=coding_pair,
            main_script=(
                _delegate(coding_pair, reason="two independent module edits"),
                read_first_ref,
                call(
                    "file_edit",
                    {
                        "path": "package/integration.py",
                        "old_string": '"""Integration surface the Main owns."""',
                        "new_string": '"""Integration surface the Main owns."""\n'
                        'INTEGRATED = "integrated two modules"\n',
                    },
                    "call-edit-1",
                ),
                say("integrated both modules"),
            ),
            child_scripts={
                MARK_ALPHA: (
                    *[
                        call(name, payload, f"call-a-{index}")
                        for index, (name, payload) in enumerate(alpha_writes)
                    ],
                    say("alpha implemented; the escape and the interpreter command were refused"),
                ),
                MARK_BETA: (
                    call(
                        "file_write",
                        {"path": "package/b.py", "content": contract_marker("beta")},
                        "call-b-1",
                    ),
                    call("file_read", {"path": "package/a.py"}, "call-b-2"),
                    say("beta implemented"),
                ),
            },
            main_write_paths=("package/integration.py",),
        ),
        Probe(
            probe_id="research_write",
            profile_id=contract.RESEARCH,
            tasks=(
                task(
                    "alpha",
                    f"{MARK_GAMMA}: implement the alpha module",
                    profile_id=contract.RESEARCH,
                    write_paths=("package/a.py",),
                ),
            ),
            main_script=(
                _delegate(
                    [
                        task(
                            "alpha",
                            f"{MARK_GAMMA}: implement the alpha module",
                            profile_id=contract.RESEARCH,
                            write_paths=("package/a.py",),
                        )
                    ]
                ),
            ),
            child_scripts={
                MARK_GAMMA: (
                    call(
                        "file_write",
                        {"path": "package/a.py", "content": contract_marker("alpha")},
                        "call-g-1",
                    ),
                    say("done"),
                )
            },
        ),
        Probe(
            probe_id="shared_write",
            profile_id=contract.CODING,
            tasks=(
                task(
                    "alpha",
                    f"{MARK_ALPHA}: extend the shared module",
                    profile_id=contract.CODING,
                    write_paths=("package/a.py",),
                ),
                task(
                    "beta",
                    f"{MARK_BETA}: also extend the shared module",
                    profile_id=contract.CODING,
                    write_paths=("package/a.py",),
                ),
            ),
            main_script=(
                _delegate(
                    [
                        task(
                            "alpha",
                            f"{MARK_ALPHA}: extend the shared module",
                            profile_id=contract.CODING,
                            write_paths=("package/a.py",),
                        ),
                        task(
                            "beta",
                            f"{MARK_BETA}: also extend the shared module",
                            profile_id=contract.CODING,
                            write_paths=("package/a.py",),
                        ),
                    ]
                ),
            ),
            child_scripts={MARK_ALPHA: (say("done"),), MARK_BETA: (say("done"),)},
        ),
        Probe(
            probe_id="single_coding",
            profile_id=contract.CODING,
            tasks=(
                task(
                    "alpha",
                    f"{MARK_ALPHA}: implement the alpha module",
                    profile_id=contract.CODING,
                    write_paths=("package/a.py",),
                ),
            ),
            main_script=(
                _delegate(
                    [
                        task(
                            "alpha",
                            f"{MARK_ALPHA}: implement the alpha module",
                            profile_id=contract.CODING,
                            write_paths=("package/a.py",),
                        )
                    ]
                ),
            ),
            child_scripts={MARK_ALPHA: (say("done"),)},
        ),
        Probe(
            probe_id="research_tool_escape",
            profile_id=contract.RESEARCH,
            tasks=research_escape_pair,
            main_script=(_delegate(research_escape_pair),),
            child_scripts={
                MARK_GAMMA: (
                    call(
                        "file_write",
                        {"path": "package/escaped.py", "content": "x = 1\n"},
                        "call-g-1",
                    ),
                    say("reported"),
                ),
                MARK_BETA: (say("reported"),),
            },
        ),
    )


def run_all() -> dict[str, Any]:
    results = [run_probe(probe) for probe in probe_table()]
    return {
        "stage": contract.STAGE_ID,
        "kind": "scripted_dynamic_path_probe",
        "not_autonomous_delegation": True,
        "probes": [
            {
                "probe_id": result.probe_id,
                "ok": result.ok,
                "checks": result.checks,
                "provider": result.provider,
                "workspace": result.workspace,
                "delegation": {
                    "route": (
                        batch.get("route") if (batch := first_batch(result.session)) else None
                    ),
                    "reason_codes": batch_reasons(first_batch(result.session)),
                    "children": [
                        {
                            "key": child.get("key"),
                            "profile_id": child.get("profile_id"),
                            "status": child.get("status"),
                            "success": child.get("success"),
                            "stop_reason": child.get("stop_reason"),
                            "error_code": child.get("error_code"),
                            "error_message": child.get("error_message"),
                            "tool_calls": child.get("tool_calls"),
                            "turns": child.get("turns"),
                        }
                        for child in _children(result.session)
                    ],
                },
                "ownership": {
                    "checked_calls": (result.session.get("ownership") or {}).get("checked_calls"),
                    "denied_calls": (result.session.get("ownership") or {}).get("denied_calls"),
                    "denials": _ownership_denials(result.session),
                },
                "handoff_reads": result.session.get("handoff_reads"),
                "tool_surface": result.session.get("tool_surface"),
                "metrics": result.session.get("metrics"),
                "child_calls": {
                    worker_id: [
                        {"tool": call.get("tool"), "result": call.get("result")}
                        for call in calls
                    ]
                    for worker_id, calls in result.conversations.items()
                },
            }
            for result in results
        ],
        "summary": {
            "probes": len(results),
            "passed": sum(1 for result in results if result.ok),
            "failed": [result.probe_id for result in results if not result.ok],
        },
    }


def first_batch(session: Mapping[str, Any]) -> dict[str, Any] | None:
    """The first delegation batch, or ``None`` when the Main never called the tool."""

    batches = _batches(session)
    return batches[0] if batches else None


__all__ = [
    "Probe",
    "ProbeResult",
    "ScriptedProvider",
    "batch_reasons",
    "first_batch",
    "probe_table",
    "run_all",
    "run_probe",
]
