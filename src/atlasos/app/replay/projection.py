"""Read-only evidence projection for the Stage H1 historical replay workbench.

Every value here is either read from a recorded artifact or explicitly reported as
``UNKNOWN`` / ``NOT_RUN`` / ``NOT_APPLICABLE`` / ``NOT_CONFIGURED`` / ``NOT_OBSERVED`` /
``UNAVAILABLE``. Nothing is inferred silently: a derived value carries its basis, and a
partial usage figure is reported as a known floor instead of a system total.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from atlasos.app.replay.sources import (
    EvidenceReader,
    EvidenceTooLarge,
    PathOutsideRoot,
    RunArtifacts,
)

UNKNOWN = "UNKNOWN"
NOT_RUN = "NOT_RUN"
NOT_APPLICABLE = "NOT_APPLICABLE"
NOT_CONFIGURED = "NOT_CONFIGURED"
NOT_OBSERVED = "NOT_OBSERVED"
UNAVAILABLE = "UNAVAILABLE"

_EMPTY = {None, "", UNKNOWN, NOT_APPLICABLE, UNAVAILABLE, NOT_RUN, NOT_CONFIGURED}

_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{8,}"), "<redacted-key>"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{8,}"), "Bearer <redacted>"),
    (
        re.compile(r"(?i)\b(?:[a-z0-9_-]*key|token|secret|password|authorization)\s*[=:]\s*\S{6,}"),
        "<redacted-credential>",
    ),
)

_ARTIFACT_ROLES = (
    "analysis",
    "evidence",
    "session",
    "native_result",
    "app_record",
    "atlas_result",
    "attempt",
    "provider_requests",
    "handoff_index",
    "worker_manifest",
    "transcript",
)

_TIMELINE_SKIP = {"user_message", "model_message"}


def scrub(text: str) -> str:
    """Remove credential-shaped substrings from anything that may reach a browser."""

    cleaned = text
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def clip(text: Any, limit: int = 240) -> str | None:
    if text is None:
        return None
    value = scrub(str(text))
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


def known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value not in _EMPTY
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def additive(values: list[Any]) -> int | float | str:
    """Sum only the components that are actually known; never invent a zero."""

    present = [number(value) for value in values if known(value)]
    real = [value for value in present if value is not None]
    if not real:
        return UNKNOWN
    return sum(real)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


class RunModel:
    """Lazily parsed artifacts for one run directory."""

    def __init__(self, run: RunArtifacts, reader: EvidenceReader) -> None:
        self.run = run
        self.reader = reader
        self.problems: list[dict[str, Any]] = []
        self._documents: dict[str, Any] = {}
        for role in _ARTIFACT_ROLES:
            relative = run.first(role)
            if relative is None:
                continue
            try:
                if role == "transcript":
                    self._documents[role] = reader.read_jsonl(relative)
                else:
                    self._documents[role] = reader.read_json(relative)
            except (
                OSError,
                json.JSONDecodeError,
                UnicodeDecodeError,
                EvidenceTooLarge,
                PathOutsideRoot,
            ) as error:
                self.problems.append(
                    {"path": relative, "reason": type(error).__name__, "detail": clip(error, 200)}
                )

    # ---------------------------------------------------------------- documents
    @property
    def analysis(self) -> dict[str, Any]:
        return self._as_dict("analysis")

    @property
    def evidence(self) -> dict[str, Any]:
        return self._as_dict("evidence")

    @property
    def session(self) -> dict[str, Any]:
        return self._as_dict("session")

    @property
    def native(self) -> dict[str, Any]:
        return self._as_dict("native_result")

    @property
    def app(self) -> dict[str, Any]:
        return self._as_dict("app_record")

    @property
    def atlas(self) -> dict[str, Any]:
        return self._as_dict("atlas_result")

    @property
    def attempt(self) -> dict[str, Any]:
        return self._as_dict("attempt")

    @property
    def worker_manifest(self) -> dict[str, Any]:
        return self._as_dict("worker_manifest")

    @property
    def handoff_index(self) -> dict[str, Any]:
        return self._as_dict("handoff_index")

    @property
    def provider_requests(self) -> Any:
        return self._documents.get("provider_requests")

    @property
    def transcript(self) -> list[dict[str, Any]]:
        rows = self._documents.get("transcript")
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _as_dict(self, role: str) -> dict[str, Any]:
        value = self._documents.get(role)
        return value if isinstance(value, dict) else {}

    def sources(self) -> list[dict[str, Any]]:
        entries = []
        for role, paths in sorted(self.run.files.items()):
            for path in paths:
                entries.append({"role": role, "path": path, "root": self.run.root_label})
        return entries

    def source_ref(self, role: str) -> dict[str, Any] | None:
        relative = self.run.first(role)
        if relative is None:
            return None
        return {"root": self.run.root_label, "path": relative}

    def read_patch_files(self) -> list[str]:
        relative = self.run.first("patch")
        if relative is None:
            return []
        try:
            head = self.reader.read_head(relative, limit=400_000)
        except (OSError, PathOutsideRoot, EvidenceTooLarge):
            return []
        names: list[str] = []
        for line in head.splitlines():
            if line.startswith("diff --git "):
                endpoint = line.rsplit(" b/", 1)
                if len(endpoint) == 2 and endpoint[1] not in names:
                    names.append(endpoint[1].strip())
        return names


# --------------------------------------------------------------------- helpers


def as_mapping(value: Any) -> dict[str, Any]:
    """A decoded JSON object, or an empty one. Evidence is untrusted input."""

    return value if isinstance(value, dict) else {}


def _nested(mapping: Any, *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _main_usage_from_session(session: dict[str, Any]) -> dict[str, Any]:
    main = _nested(session, "main") or {}
    metrics = _nested(session, "metrics") or {}
    return {
        "prompt_tokens": main.get("total_prompt_tokens"),
        "cache_hit_tokens": main.get("cache_hit_tokens"),
        "cache_miss_tokens": main.get("cache_miss_tokens"),
        "completion_tokens": main.get("completion_tokens"),
        "peak_context_tokens": main.get("peak_context_tokens"),
        "requests": main.get("requests") or metrics.get("main_requests"),
    }


def _child_usage_from_session(session: dict[str, Any]) -> dict[str, Any]:
    child = _nested(session, "child_tokens") or {}
    return {
        "prompt_tokens": child.get("total_prompt_tokens"),
        "cache_hit_tokens": child.get("cache_hit_tokens"),
        "cache_miss_tokens": child.get("cache_miss_tokens"),
        "completion_tokens": child.get("completion_tokens"),
        "children_started": child.get("children_started"),
        "children_completed": child.get("children_completed"),
        "children_failed": child.get("children_failed"),
        "children_timed_out": child.get("children_timed_out"),
    }


def _product_absence(session: dict[str, Any]) -> Any:
    """A product run has no static acceptance contract; that is ``NOT_CONFIGURED``, not UNKNOWN."""

    return NOT_CONFIGURED if session and "acceptance" in session else None


def _public_test_absence(session: dict[str, Any]) -> Any:
    """A run with no host-fixed suite says so; ``status`` is the field the goal path writes."""

    block = _nested(session, "public_tests")
    if not isinstance(block, dict):
        return None
    status = block.get("status")
    if isinstance(status, str) and status:
        return status
    if block.get("configured") is False:
        return NOT_CONFIGURED
    return None


def _integrity_axis(session: dict[str, Any]) -> Any:
    """What the session says about execution integrity, using its own vocabulary.

    The goal path records a corpus (``mode: record``); that is evidence capture, not a PASS verdict,
    so it is reported as ``RECORDED`` and never as a deliverable result.
    """

    block = _nested(session, "execution_integrity")
    if not isinstance(block, dict) or not block:
        return None
    status = block.get("status")
    if isinstance(status, str) and status:
        return status
    mode = block.get("mode")
    if isinstance(mode, str) and mode:
        return "RECORDED" if mode.startswith("record") else mode.upper()
    return None


def _pick(*candidates: tuple[Any, str]) -> tuple[Any, str]:
    """First recorded value with the name of the field it came from."""

    for value, source in candidates:
        if known(value):
            return value, source
    return UNKNOWN, UNAVAILABLE


def _verification_axes(model: RunModel) -> dict[str, Any]:
    analysis = model.analysis
    evidence = model.evidence
    session = model.session
    outcome = _nested(analysis, "outcome") or {}
    correctness = _nested(evidence, "correctness") or {}
    public_runs = _nested(session, "public_tests", "runs") or []
    last_public = public_runs[-1] if public_runs else None
    verification = _nested(session, "verification") or {}

    # The raw loop outcome. ``raw_agent_success`` is the newer name of the same fact;
    # ``agent_success`` is kept as the fallback so evidence written before the split still reads.
    raw_success, raw_source = _pick(
        (correctness.get("raw_agent_success"), "evidence.correctness.raw_agent_success"),
        (correctness.get("agent_success"), "evidence.correctness.agent_success"),
        (outcome.get("raw_agent_success"), "analysis.outcome.raw_agent_success"),
        (outcome.get("agent_success"), "analysis.outcome.agent_success"),
        (_nested(session, "main", "success"), "session.main.success"),
        (_nested(model.native, "agent_success"), "native result agent_success"),
        (
            _nested(model.app, "status") == "COMPLETED" if model.app else None,
            "app run.json status",
        ),
    )
    # Effective completion answers "did the execution finish the work it claimed", and it is its
    # own recorded value: the evidence's ``primary_agent_completion`` when the cell recorded one,
    # otherwise the session's own ``execution_succeeded``. It is never derived from the stop
    # reason (a stop reason is not a boolean) and never from delivered success.
    effective, effective_source = _pick(
        (
            correctness.get("primary_agent_completion"),
            "evidence.correctness.primary_agent_completion",
        ),
        (_nested(session, "execution_succeeded"), "session.execution_succeeded"),
        (outcome.get("execution_succeeded"), "analysis.outcome.execution_succeeded"),
    )
    axes: dict[str, Any] = {
        "raw_agent_success": raw_success,
        "effective_agent_completion": _recorded_or(effective, UNAVAILABLE),
        "public_test": _first_known(
            correctness.get("public_test"),
            outcome.get("public_test_status"),
            _nested(last_public, "status"),
        ),
        "static_acceptance": _first_known(
            correctness.get("static_acceptance"),
            outcome.get("static_acceptance"),
            verification.get("static_status"),
        ),
        "independent_oracle": _first_known(
            (correctness.get("independent_oracle") or {}).get("status")
            if isinstance(correctness.get("independent_oracle"), dict)
            else None,
            outcome.get("oracle_status"),
            _nested(model.native, "oracle", "status"),
        ),
        "execution_integrity": _first_known(
            correctness.get("execution_integrity"), outcome.get("execution_integrity")
        ),
        "delivered_success": _first_known(
            correctness.get("delivered_success"),
            outcome.get("delivered_success"),
            outcome.get("goal_correctness"),
            model.native.get("success"),
        ),
    }
    # A product run states these on its session, and "not configured" is a real answer rather
    # than an unknown: no independent oracle existed, so nothing was verified either way.
    _settle(axes, "independent_oracle", _nested(session, "acceptance"))
    _settle(axes, "public_test", _public_test_absence(session))
    _settle(axes, "static_acceptance", _product_absence(session))
    _settle(axes, "execution_integrity", _integrity_axis(session))
    axes["axis_sources"] = {
        "raw_agent_success": raw_source
        if raw_success is not UNKNOWN
        else "no raw success field recorded",
        "effective_agent_completion": effective_source
        if known(effective)
        else "no completion field recorded",
    }
    return axes


def _settle(axes: dict[str, Any], name: str, fallback: Any) -> None:
    """Fill an axis from a fallback that may itself be a status token such as NOT_CONFIGURED."""

    if known(axes.get(name)):
        return
    if isinstance(fallback, str) and fallback.strip():
        axes[name] = fallback


def _first_known(*values: Any) -> Any:
    for value in values:
        if known(value):
            return value
    return UNKNOWN


def _recorded_or(value: Any, fallback: str) -> Any:
    """A recorded value, or the explicit "nothing was recorded" token."""

    return value if known(value) else fallback


NO_EXECUTION_EVIDENCE = "NO_EXECUTION_EVIDENCE"


def _has_execution_evidence(model: RunModel) -> bool:
    return bool(model.session or model.native or model.evidence or model.app or model.atlas)


def _headline(axes: dict[str, Any], *, executed: bool) -> str:
    if not executed:
        # An analysis file on its own never establishes that anything was delivered.
        return NO_EXECUTION_EVIDENCE
    oracle = axes.get("independent_oracle")
    integrity = axes.get("execution_integrity")
    delivered = axes.get("delivered_success")
    if oracle == NOT_CONFIGURED:
        # Nothing verified this run. Saying "delivered" would be an unsupported claim, and saying
        # "not delivered" would be a verdict nobody issued.
        if axes.get("effective_agent_completion") is True:
            return "COMPLETED_UNVERIFIED"
        if axes.get("effective_agent_completion") is False:
            return "NOT_COMPLETED"
        return "INCOMPLETE"
    if oracle == "FAIL" or integrity == "FAIL" or delivered is False:
        return "NOT_DELIVERED"
    if oracle == "PASS" and integrity in {"PASS"} and delivered in {True, "PASS"}:
        return "DELIVERED_AND_VERIFIED"
    if not known(oracle) or not known(integrity):
        return "INCOMPLETE"
    return "MIXED"


# --------------------------------------------------------------------- summary


def summary(model: RunModel) -> dict[str, Any]:
    analysis = model.analysis
    evidence = model.evidence
    session = model.session
    native = model.native
    app = model.app
    atlas = model.atlas
    outcome = _nested(analysis, "outcome") or {}
    usage = _nested(evidence, "usage") or {}
    correctness = _nested(evidence, "correctness") or {}
    context = _nested(evidence, "context") or {}
    metrics = _nested(session, "metrics") or {}
    identity = _nested(native, "identity") or {}
    run = model.run

    plan_id = _first_known(
        analysis.get("plan_id"),
        evidence.get("plan_id"),
        _nested(model.app, "mode") if model.app else None,
        _nested(model.atlas, "run_id"),
    )
    if plan_id is UNKNOWN and not (analysis or evidence or session or native or app or atlas):
        plan_id = UNAVAILABLE

    task_id = _first_known(
        analysis.get("task_id"),
        evidence.get("task_id"),
        _nested(model.app, "mode") and _nested(app, "run_id"),
        _nested(native, "sample_id"),
    )
    requested_model = _first_known(
        usage.get("requested_model"),
        session.get("model"),
        identity.get("model"),
        _nested(app, "model"),
        _nested(atlas, "model"),
    )
    returned_model = _first_known(
        usage.get("returned_model"),
        _nested(session, "verification", "returned_model"),
        UNKNOWN,
    )

    mode, mode_source = _execution_mode(model)

    started_at, finished_at, duration_s, timing_basis = _timing(model)

    main_usage = _usage_block(model)
    agents = agent_views(model)
    children_started = _nested(session, "delegation", "totals", "children_started")

    axes = _verification_axes(model)
    matrix = run.matrix_row or {}
    cost = _cost(model, matrix)

    recorded_status = _first_known(
        matrix.get("status"),
        outcome.get("status"),
        session.get("status"),
        app.get("status"),
        atlas.get("status"),
        _nested(native, "failure_class") and "failed",
    )
    payload = {
        "run_key": run.run_key,
        "source_kind": run.kind,
        "run_dir": run.run_dir,
        "evidence_root": run.root_label,
        "cell_id": run.cell_id,
        "experiment": {
            "plan_id": plan_id,
            "scope": _first_known(analysis.get("scope"), evidence.get("scope"), UNAVAILABLE),
            "batch_id": _first_known(evidence.get("batch_id"), UNAVAILABLE),
            "stage": run.root_label,
            "matrix_stage": _first_known(matrix.get("stage")),
        },
        "task": {
            "task_id": task_id,
            "family": _first_known(evidence.get("family"), UNAVAILABLE),
            "repetition": _first_known(analysis.get("repetition"), evidence.get("repetition")),
        },
        "execution": {
            "mode": mode,
            "mode_source": mode_source,
            "arm_id": _first_known(analysis.get("arm_id"), evidence.get("arm_id")),
            "profile_id": _first_known(outcome.get("profile_id"), UNAVAILABLE),
            "exposure": _first_known(outcome.get("exposure"), UNAVAILABLE),
            "usage_attribution": _first_known(evidence.get("usage_attribution"), UNAVAILABLE),
            "provider": _first_known(
                _nested(session, "sandbox", "provider"),
                _nested(native, "identity", "provider"),
                UNAVAILABLE,
            ),
        },
        "model": {
            "requested": requested_model,
            "returned": returned_model,
            "requested_source": _model_source(model),
            "provider": _first_known(
                session.get("provider"),
                _nested(native, "identity", "provider"),
                UNAVAILABLE,
            ),
            "provider_kind": _provider_kind(
                _first_known(
                    session.get("provider"),
                    _nested(native, "identity", "provider"),
                    None,
                )
            ),
        },
        "status": {
            "recorded": recorded_status,
            "failure_kind": _first_known(
                matrix.get("failure_kind"),
                outcome.get("failure_kind"),
                correctness.get("failure_kind"),
                session.get("failure"),
                native.get("failure_class"),
                UNAVAILABLE,
            ),
            "verdict": _first_known(
                matrix.get("executor_verdict"), outcome.get("executor_verdict"), UNAVAILABLE
            ),
            "headline": _headline(axes, executed=_has_execution_evidence(model)),
        },
        "timing": {
            "started_at": iso(started_at),
            "finished_at": iso(finished_at),
            "duration_s": duration_s,
            "basis": timing_basis,
        },
        "counts": {
            "agents": len(agents),
            "children_started": _first_known(children_started, 0 if session else UNAVAILABLE),
            "delegation_rounds": _first_known(
                metrics.get("delegation_rounds"), _nested(evidence, "delegation", "rounds")
            ),
            "max_observed_child_concurrency": _first_known(
                metrics.get("max_observed_child_concurrency"),
                _nested(evidence, "delegation", "max_observed_concurrency"),
            ),
            "model_requests": _first_known(
                main_usage.get("requests"), _nested(usage, "requests", "total")
            ),
            "tool_calls": _first_known(
                _nested(session, "main", "tool_calls") and len(session["main"]["tool_calls"]),
                _nested(native, "tool_calls"),
            ),
        },
        "usage": main_usage,
        "cost": cost,
        "verification": axes,
        "context": {
            "main_peak_context": _first_known(
                metrics.get("main_peak_context_tokens"),
                usage.get("main", {}).get("peak_context_tokens")
                if isinstance(usage.get("main"), dict)
                else None,
            ),
            "system_max_agent_context": _first_known(context.get("system_max_agent_context")),
        },
        "completeness": _completeness(model),
        "sources": model.sources(),
        "warnings": _warnings(model),
        "problems": model.problems,
    }
    if matrix:
        payload["matrix"] = {
            "verdict": matrix.get("verdict"),
            "executor_verdict": matrix.get("executor_verdict"),
            "cost_usd": matrix.get("cost_usd"),
            "oracle_status": matrix.get("oracle_status"),
            "source": run.matrix_ref,
        }
    else:
        payload["matrix"] = None
    return payload


def _execution_mode(model: RunModel) -> tuple[str, str]:
    evidence = model.evidence
    declared = evidence.get("execution_mode")
    if isinstance(declared, str) and declared:
        return declared, "declared"
    app = model.app
    if app and app.get("mode"):
        # A product run declares its mode on the application record: ``main_direct`` and
        # ``main_dynamic`` are two different executions of the same runtime and must stay distinct.
        return str(app["mode"]), "declared: app run record"
    session = model.session
    if session:
        if session.get("mode"):
            return str(session["mode"]), "declared"
        if session.get("delegation_enabled") is False:
            return "single_agent", "derived: delegation disabled in session"
        return "main_driven", "derived: main-driven session"
    native = model.native
    if native:
        return str(native.get("mode") or "native"), "declared: native runner result"
    atlas = model.atlas
    if atlas:
        return "static_dag", "derived: AtlasRuntime workflow result"
    return UNKNOWN, "no evidence"


_LIVE_PROVIDERS = {"deepseek", "openai", "anthropic", "google", "gemini", "azure"}
_SCRIPTED_PROVIDERS = {"scripted", "fake", "stub", "deterministic", "offline"}


def _provider_kind(provider: Any) -> str:
    if not isinstance(provider, str) or not provider:
        return UNKNOWN
    lowered = provider.lower()
    if any(name in lowered for name in _SCRIPTED_PROVIDERS):
        return "SCRIPTED"
    if any(name in lowered for name in _LIVE_PROVIDERS):
        return "LIVE_MODEL"
    return UNKNOWN


def _model_source(model: RunModel) -> str:
    if _nested(model.evidence, "usage", "requested_model"):
        return "cell evidence"
    if model.session.get("model"):
        return "session.json"
    if _nested(model.native, "identity", "model"):
        return "native result identity"
    if model.app.get("model") or model.atlas.get("model"):
        return "run result"
    return "no evidence"


def _timing(model: RunModel) -> tuple[datetime | None, datetime | None, float | None, str]:
    native = model.native
    if native.get("started_at") or native.get("finished_at"):
        start = parse_time(native.get("started_at")) or parse_time(
            _nested(native, "identity", "started_at")
        )
        end = parse_time(native.get("finished_at"))
        basis = "native result started_at/finished_at"
        return start, end, seconds_between(start, end), basis
    attempt = model.attempt
    analysis = model.analysis
    duration = number(analysis.get("duration_s"))
    claim = parse_time(attempt.get("claimed_at"))
    if claim is not None and duration is not None:
        end = claim + timedelta(seconds=duration)
        return claim, end, duration, "attempt.claimed_at + analysis.duration_s (derived)"
    app = model.app
    if app.get("created_at"):
        start = parse_time(app.get("created_at"))
        end = parse_time(app.get("updated_at"))
        return start, end, seconds_between(start, end), "app run record timestamps"
    if claim is not None:
        return claim, None, None, "attempt.claimed_at only"
    return None, None, duration if duration is not None else None, "no timestamp recorded"


def _usage_block(model: RunModel) -> dict[str, Any]:
    evidence_usage = _nested(model.evidence, "usage") or {}
    session = model.session
    analysis_usage = _nested(model.analysis, "outcome", "usage") or {}
    native = model.native

    if evidence_usage:
        main_block = evidence_usage.get("main")
        child_block = evidence_usage.get("children")
        primary = evidence_usage.get("primary_agent")
        if isinstance(main_block, dict):
            prompt = main_block.get("prompt_tokens")
            completion = main_block.get("completion_tokens")
            cache_hit = main_block.get("cache_hit_tokens")
            cache_miss = main_block.get("cache_miss_tokens")
            peak = None
            attribution = "main"
        elif isinstance(primary, dict):
            prompt = primary.get("prompt_tokens")
            completion = primary.get("completion_tokens")
            cache_hit = primary.get("cache_hit_tokens")
            cache_miss = primary.get("cache_miss_tokens")
            peak = _nested(model.evidence, "context", "primary_agent_peak_context")
            attribution = "primary_agent"
        elif main_block == NOT_APPLICABLE:
            prompt = completion = cache_hit = cache_miss = peak = NOT_APPLICABLE
            attribution = "none: no Atlas main agent in this arm"
        else:
            prompt = completion = cache_hit = cache_miss = peak = None
            attribution = "unknown"
        if isinstance(child_block, dict):
            child_prompt: Any = child_block.get("prompt_tokens")
            child_completion: Any = child_block.get("completion_tokens")
        elif child_block == NOT_APPLICABLE:
            # An arm with no child agents has no child usage; that is not an unknown value.
            child_prompt = child_completion = NOT_APPLICABLE
        else:
            child_prompt = child_completion = None
        # A child reporting zero tokens does not establish a run-level floor when the primary
        # agent never reported usage: that run's consumption is UNKNOWN, not zero.
        primary_known = known(prompt) or known(completion)
        floor = (
            _floor([prompt, child_prompt, completion, child_completion])
            if primary_known
            else UNKNOWN
        )
        return {
            "attribution": attribution,
            "main_prompt_tokens": _value_or_unknown(prompt),
            "main_completion_tokens": _value_or_unknown(completion),
            "child_prompt_tokens": _value_or_unknown(child_prompt),
            "child_completion_tokens": _value_or_unknown(child_completion),
            "cache_hit_tokens": _or_unknown(cache_hit),
            "cache_miss_tokens": _or_unknown(cache_miss),
            "system_total_tokens": _total_or_unknown(
                evidence_usage.get("system_total_tokens"), floor
            ),
            "known_floor_tokens": floor,
            "main_peak_context": _or_unknown(peak),
            "requests": evidence_usage.get("requests") or UNKNOWN,
            "basis": "stage-f cell evidence usage block",
        }

    if analysis_usage:
        prompt = analysis_usage.get("main_cumulative_input_tokens")
        completion = analysis_usage.get("main_completion_tokens")
        child_prompt = analysis_usage.get("child_total_prompt_tokens")
        child_completion = analysis_usage.get("child_completion_tokens")
        total = analysis_usage.get("system_total_tokens")
        metrics = _nested(session, "metrics") or {}
        floor = _floor([prompt, child_prompt, completion, child_completion])
        return {
            "attribution": "main_child",
            "main_prompt_tokens": _or_unknown(prompt),
            "main_completion_tokens": _or_unknown(completion),
            "child_prompt_tokens": _or_unknown(child_prompt),
            "child_completion_tokens": _or_unknown(child_completion),
            "cache_hit_tokens": _or_unknown(metrics.get("main_cache_hit_tokens")),
            "cache_miss_tokens": _or_unknown(metrics.get("main_cache_miss_tokens")),
            "system_total_tokens": _total_or_unknown(total, floor),
            "known_floor_tokens": floor,
            "main_peak_context": _or_unknown(metrics.get("main_peak_context_tokens")),
            "requests": _or_unknown(metrics.get("main_requests")),
            "basis": "cell analysis outcome usage",
        }

    if session:
        main = _main_usage_from_session(session)
        child = _child_usage_from_session(session)
        total = _nested(session, "metrics", "system_total_tokens")
        floor = _floor(
            [
                main["prompt_tokens"],
                child["prompt_tokens"],
                main["completion_tokens"],
                child["completion_tokens"],
            ]
        )
        return {
            "attribution": "main_child",
            "main_prompt_tokens": _or_unknown(main["prompt_tokens"]),
            "main_completion_tokens": _or_unknown(main["completion_tokens"]),
            "child_prompt_tokens": _or_unknown(child["prompt_tokens"]),
            "child_completion_tokens": _or_unknown(child["completion_tokens"]),
            "cache_hit_tokens": _or_unknown(main["cache_hit_tokens"]),
            "cache_miss_tokens": _or_unknown(main["cache_miss_tokens"]),
            "system_total_tokens": _total_or_unknown(total, floor),
            "known_floor_tokens": floor,
            "main_peak_context": _or_unknown(main["peak_context_tokens"]),
            "requests": _or_unknown(main["requests"]),
            "basis": "main-driven session usage",
        }

    if native:
        budget = _nested(native, "metrics", "budget") or {}
        prompt = budget.get("used_prompt_tokens")
        completion = budget.get("used_completion_tokens")
        primary = _primary_native_usage(native)
        if known(primary.get("prompt_tokens")):
            prompt, completion = primary["prompt_tokens"], primary["completion_tokens"]
        return {
            "attribution": "primary_agent",
            "main_prompt_tokens": prompt if known(prompt) else NOT_APPLICABLE,
            "main_completion_tokens": completion if known(completion) else NOT_APPLICABLE,
            "child_prompt_tokens": NOT_APPLICABLE,
            "child_completion_tokens": NOT_APPLICABLE,
            "cache_hit_tokens": _or_unknown(primary.get("cache_hit_tokens")),
            "cache_miss_tokens": _or_unknown(primary.get("cache_miss_tokens")),
            "system_total_tokens": _total_or_unknown(
                budget.get("used_tokens"), _floor([prompt, completion])
            ),
            "known_floor_tokens": _floor([prompt, completion]),
            "main_peak_context": UNAVAILABLE,
            "requests": _or_unknown(budget.get("requests_completed")),
            "basis": "native runner budget block",
        }

    if model.app or model.atlas:
        source = model.app or model.atlas
        metrics = _nested(source, "metrics") or {}
        prompt = metrics.get("prompt_tokens")
        completion = metrics.get("completion_tokens")
        return {
            "attribution": "workflow",
            "main_prompt_tokens": _or_unknown(prompt),
            "main_completion_tokens": _or_unknown(completion),
            "child_prompt_tokens": NOT_APPLICABLE,
            "child_completion_tokens": NOT_APPLICABLE,
            "cache_hit_tokens": _or_unknown(metrics.get("cache_hit_tokens")),
            "cache_miss_tokens": _or_unknown(metrics.get("cache_miss_tokens")),
            "system_total_tokens": _or_unknown(
                additive([prompt, completion]) if known(prompt) or known(completion) else None
            ),
            "known_floor_tokens": _floor([prompt, completion]),
            "main_peak_context": UNAVAILABLE,
            "requests": _or_unknown(metrics.get("requests")),
            "basis": "workflow result metrics",
        }

    return {
        "attribution": "unknown",
        "main_prompt_tokens": UNAVAILABLE,
        "main_completion_tokens": UNAVAILABLE,
        "child_prompt_tokens": UNAVAILABLE,
        "child_completion_tokens": UNAVAILABLE,
        "cache_hit_tokens": UNAVAILABLE,
        "cache_miss_tokens": UNAVAILABLE,
        "system_total_tokens": UNAVAILABLE,
        "known_floor_tokens": UNAVAILABLE,
        "main_peak_context": UNAVAILABLE,
        "requests": UNAVAILABLE,
        "basis": "no usage evidence",
    }


def _primary_native_usage(native: dict[str, Any]) -> dict[str, Any]:
    workers = native.get("worker_results")
    if not isinstance(workers, dict):
        return {}
    for value in workers.values():
        if isinstance(value, dict) and isinstance(value.get("token_usage"), dict):
            usage = value["token_usage"]
            return {
                "prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens"),
                "cache_hit_tokens": usage.get("cache_read_input_tokens"),
                "cache_miss_tokens": usage.get("input_tokens"),
            }
    return {}


def _or_unknown(value: Any) -> Any:
    return value if known(value) else UNKNOWN


def _value_or_unknown(value: Any) -> Any:
    """Keep an explicit evidence token (``NOT_APPLICABLE``) instead of rewriting it."""

    return UNKNOWN if value is None else value


def _total_or_unknown(total: Any, floor: Any) -> Any:
    """A recorded ``0`` next to unknown components is not a measured zero."""

    if not known(total):
        return UNKNOWN
    if number(total) == 0 and not known(floor):
        return UNKNOWN
    return total


def _floor(values: list[Any]) -> Any:
    total = additive(values)
    return total


def _cost(model: RunModel, matrix: dict[str, Any]) -> dict[str, Any]:
    outcome = _nested(model.analysis, "outcome") or {}
    value = _first_known(outcome.get("cost_usd"), matrix.get("cost_usd"))
    if value is UNKNOWN:
        return {"estimated_usd": UNKNOWN, "basis": "no cost recorded", "is_floor": False}
    basis = "frozen pricing configuration recorded by the executor"
    return {
        "estimated_usd": value,
        "basis": basis,
        "is_floor": bool(matrix.get("cost_basis") == "observed_floor"),
    }


def _warnings(model: RunModel) -> list[str]:
    warnings: list[str] = []
    if not _has_execution_evidence(model):
        warnings.append(
            "analysis-only directory: no session, native result or cell evidence was recorded "
            "here, so the recorded verdicts are not backed by an observed execution"
        )
    evidence_usage = _nested(model.evidence, "usage") or {}
    if evidence_usage and not known(evidence_usage.get("system_total_tokens")):
        warnings.append("system token usage is UNKNOWN in the cell evidence")
    if evidence_usage and evidence_usage.get("main") == NOT_APPLICABLE:
        warnings.append("main-agent usage is NOT_APPLICABLE: this arm has no Atlas main agent")
    session = model.session
    if session and session.get("status") == "failed":
        warnings.append("the runtime reported a failed session; the projection keeps that status")
    unsupported = [f"{problem['path']} ({problem['reason']})" for problem in model.problems]
    warnings.extend(f"unreadable evidence: {item}" for item in unsupported)
    return warnings


def _completeness(model: RunModel) -> dict[str, Any]:
    facets: dict[str, str] = {}
    facets["run_identity"] = "PRESENT" if _has_identity(model) else "MISSING"
    facets["agents"] = "PRESENT" if agent_views(model) else "MISSING"
    usage = _usage_block(model)
    if known(usage.get("system_total_tokens")):
        facets["usage"] = "PRESENT"
    elif known(usage.get("main_prompt_tokens")) or known(usage.get("child_prompt_tokens")):
        facets["usage"] = "PARTIAL"
    else:
        facets["usage"] = "MISSING"
    axes = _verification_axes(model)
    facets["verification"] = (
        "PRESENT"
        if known(axes.get("independent_oracle")) or known(axes.get("public_test"))
        else "MISSING"
    )
    facets["delegation"] = "PRESENT" if _delegation_present(model) else "MISSING"
    facets["requests"] = "PRESENT" if _request_rows(model) else "MISSING"
    facets["timeline"] = "PRESENT" if model.transcript or model.attempt else "MISSING"
    facets["artifacts"] = (
        "PRESENT" if model.run.has("patch") or model.run.has("handoff_index") else "MISSING"
    )
    present = sum(1 for value in facets.values() if value == "PRESENT")
    partial = sum(1 for value in facets.values() if value == "PARTIAL")
    if present == len(facets):
        level = "COMPLETE"
    elif present + partial >= 4:
        level = "PARTIAL"
    elif present + partial >= 1:
        level = "MINIMAL"
    else:
        level = "UNSUPPORTED"
    missing = [name for name, value in facets.items() if value == "MISSING"]
    return {
        "level": level,
        "facets": facets,
        "present": present,
        "total": len(facets),
        "missing": missing,
    }


def _has_identity(model: RunModel) -> bool:
    return bool(
        model.analysis.get("cell_id")
        or model.evidence.get("cell_id")
        or model.session.get("run_id")
        or model.native.get("run_dir")
        or model.app.get("run_id")
        or model.atlas.get("run_id")
    )


def _delegation_present(model: RunModel) -> bool:
    session = model.session
    if not session:
        return False
    enabled = session.get("delegation_enabled")
    batches = _nested(session, "delegation", "batches")
    return bool(enabled) or bool(batches)


def _request_journal(model: RunModel) -> tuple[list[dict[str, Any]] | None, dict[str, Any], str]:
    """Read the producer's records or the supported older envelopes and lists."""
    candidates = (
        (model.provider_requests, "provider_requests.json"),
        (model.session.get("model_requests"), "session.json#model_requests"),
    )
    for raw, source in candidates:
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)], {}, source
        if isinstance(raw, dict):
            for key in ("records", "requests", "entries"):
                rows = raw.get(key)
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)], raw, source
    return None, {}, candidates[0 if model.provider_requests is not None else 1][1]


def _request_rows(model: RunModel) -> list[dict[str, Any]]:
    return _request_journal(model)[0] or []


# ---------------------------------------------------------------------- agents


def agent_views(model: RunModel) -> list[dict[str, Any]]:
    agents: list[dict[str, Any]] = []
    session = model.session
    if session:
        agents.extend(_session_agents(model))
    native = model.native
    if native:
        agents.extend(_native_agents(native))
    source = model.app or model.atlas
    if source and isinstance(source.get("tasks"), list):
        for task in source["tasks"]:
            if not isinstance(task, dict):
                continue
            agents.append(
                {
                    "agent_id": str(
                        task.get("assignee") or task.get("agent") or task.get("task_id")
                    ),
                    "role": "DAG_NODE",
                    "profile_id": "workflow",
                    "parent": None,
                    "task_id": task.get("task_id"),
                    "objective": clip(task.get("title"), 240),
                    "owned_paths": [],
                    "dependencies": list(task.get("depends_on") or []),
                    "started_at": None,
                    "finished_at": None,
                    "stop_reason": task.get("status"),
                    "raw_success": task.get("status") == "completed",
                    "effective_success": UNKNOWN,
                    "usage": {
                        "prompt_tokens": _or_unknown(task.get("prompt_tokens")),
                        "completion_tokens": _or_unknown(task.get("completion_tokens")),
                    },
                    "source": "workflow result tasks",
                }
            )
    return agents


def _session_agents(model: RunModel) -> list[dict[str, Any]]:
    session = model.session
    main = _nested(session, "main") or {}
    ownership = _nested(session, "ownership", "workers") or {}
    main_id = str(main.get("worker_id") or "atlas-main")
    first_ts, last_ts = _trajectory_span(model)
    agents: list[dict[str, Any]] = [
        {
            "agent_id": str(main.get("agent") or "atlas-main"),
            "role": "MAIN",
            "profile_id": "main",
            "parent": None,
            "task_id": main.get("task_id"),
            "objective": clip(session.get("goal"), 400),
            "owned_paths": list((ownership.get(main_id) or {}).get("paths") or []),
            "dependencies": [],
            "started_at": iso(first_ts),
            "finished_at": iso(last_ts),
            "stop_reason": _first_known(main.get("stop_reason"), UNAVAILABLE),
            "raw_success": main.get("success"),
            "effective_success": _recorded_or(
                _nested(model.evidence, "correctness", "primary_agent_completion"), UNAVAILABLE
            ),
            "worker_id": main_id,
            "usage": {
                "prompt_tokens": _or_unknown(main.get("total_prompt_tokens")),
                "completion_tokens": _or_unknown(main.get("completion_tokens")),
                "cache_hit_tokens": _or_unknown(main.get("cache_hit_tokens")),
                "cache_miss_tokens": _or_unknown(main.get("cache_miss_tokens")),
                "peak_context_tokens": _or_unknown(main.get("peak_context_tokens")),
                "requests": _or_unknown(main.get("requests")),
            },
            "turns": main.get("turns"),
            "tool_calls": len(main.get("tool_calls") or []),
            "source": "session.json main block",
        }
    ]
    manifest = _nested(model.worker_manifest, "workers") or {}
    origin = parse_time(_nested(session, "delegation", "started_at"))
    limits = _nested(session, "delegation", "limits") or {}
    for batch in _nested(session, "delegation", "batches") or []:
        if not isinstance(batch, dict):
            continue
        for child in batch.get("children") or []:
            if not isinstance(child, dict):
                continue
            worker_id = str(child.get("worker_id") or child.get("key") or "")
            assignment = (manifest.get(worker_id) or {}).get("assignment") or {}
            started = _offset_time(origin, child.get("started_offset_ms"))
            finished = _offset_time(origin, child.get("finished_offset_ms"))
            agents.append(
                {
                    "agent_id": worker_id,
                    "role": "CODING_WORKER"
                    if child.get("profile_id") == "coding_worker"
                    else "WORKER",
                    "profile_id": child.get("profile_id"),
                    "parent": str(main.get("agent") or "atlas-main"),
                    "task_id": child.get("key"),
                    "objective": clip(assignment.get("task"), 400),
                    "owned_paths": list((ownership.get(worker_id) or {}).get("paths") or []),
                    "dependencies": [],
                    "started_at": iso(started),
                    "finished_at": iso(finished),
                    "stop_reason": _first_known(child.get("stop_reason"), UNAVAILABLE),
                    "raw_success": child.get("success"),
                    "effective_success": UNAVAILABLE,
                    "usage": {
                        "prompt_tokens": _or_unknown(child.get("total_prompt_tokens")),
                        "completion_tokens": _or_unknown(child.get("completion_tokens")),
                        "cache_hit_tokens": _or_unknown(child.get("cache_hit_tokens")),
                        "cache_miss_tokens": _or_unknown(child.get("cache_miss_tokens")),
                    },
                    "turns": child.get("turns"),
                    "tool_calls": child.get("tool_calls"),
                    "max_turns": limits.get("child_max_turns"),
                    "handoff_ref": child.get("ref_id"),
                    "error": child.get("error_code"),
                    "source": "delegation batch children",
                }
            )
    return agents


def _native_agents(native: dict[str, Any]) -> list[dict[str, Any]]:
    workers = native.get("worker_results")
    if not isinstance(workers, dict):
        return []
    start = parse_time(native.get("started_at")) or parse_time(
        _nested(native, "identity", "started_at")
    )
    end = parse_time(native.get("finished_at"))
    agents = []
    for name, value in workers.items():
        if not isinstance(value, dict):
            continue
        usage = as_mapping(value.get("token_usage"))
        agents.append(
            {
                "agent_id": str(name),
                "role": "NATIVE",
                "profile_id": "anycode-native",
                "parent": None,
                "task_id": _nested(native, "identity", "sample_id"),
                "objective": clip(_nested(native, "task"), 400),
                "owned_paths": [],
                "dependencies": [],
                "started_at": iso(start),
                "finished_at": iso(end),
                "stop_reason": _first_known(value.get("stop_reason"), UNAVAILABLE),
                "raw_success": value.get("success"),
                "effective_success": _first_known(_nested(native, "success"), UNAVAILABLE),
                "usage": {
                    "prompt_tokens": _or_unknown(usage.get("input_tokens")),
                    "completion_tokens": _or_unknown(usage.get("output_tokens")),
                    "cache_hit_tokens": _or_unknown(usage.get("cache_read_input_tokens")),
                },
                "turns": value.get("turns"),
                "tool_calls": value.get("tool_calls"),
                "source": "native result worker_results",
            }
        )
    return agents


def _offset_time(origin: datetime | None, offset_ms: Any) -> datetime | None:
    value = number(offset_ms)
    if origin is None or value is None:
        return None
    return origin + timedelta(milliseconds=float(value))


def _trajectory_span(model: RunModel) -> tuple[datetime | None, datetime | None]:
    times = [
        stamp
        for stamp in (parse_time(row.get("ts")) for row in model.transcript)
        if stamp is not None
    ]
    if not times:
        return None, None
    return min(times), max(times)


# -------------------------------------------------------------------- timeline


def timeline(model: RunModel, *, limit: int = 2000, offset: int = 0) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    events.extend(_transcript_events(model))
    events.extend(_delegation_events(model))
    events.extend(_cell_lifecycle_events(model))
    events.extend(_native_events(model))
    events.extend(_app_events(model))
    events.extend(_verification_events(model))

    observed = [event for event in events if event["timestamp"]]
    ordered = [event for event in events if not event["timestamp"]]
    observed.sort(key=lambda item: (item["timestamp"], item["order"]))
    ordered.sort(key=lambda item: item["order"])
    merged = observed + ordered
    for index, event in enumerate(merged):
        event["sequence"] = index
        event["event_id"] = f"{model.run.run_key}:{event['source']}:{event['local_id']}"
        event.pop("local_id", None)
    total = len(merged)
    window = merged[offset : offset + limit]
    return {
        "run_key": model.run.run_key,
        "timeline_mode": "observed_then_ordered" if ordered else "observed",
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": offset + limit < total,
        "counts": {
            "observed": len(observed),
            "order_only": len(ordered),
            "skipped_messages": sum(
                1
                for row in model.transcript
                if row.get("payload") and _event_type(row) in _TIMELINE_SKIP
            ),
        },
        "events": window,
        "notes": [
            "observed events carry a recorded timestamp; order-only events are evidence without "
            "a timestamp and are never given a synthetic one",
            "concurrency is only reported from recorded offsets; no DAG is assumed",
        ],
    }


def _event_type(row: dict[str, Any]) -> str:
    payload = row.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("atlas_trajectory"), dict):
        return str(payload["atlas_trajectory"].get("event_type") or "")
    return str(row.get("kind") or "")


def _transcript_events(model: RunModel) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    run_id = model.run.cell_id or model.session.get("run_id") or model.run.run_key
    for row in model.transcript:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        envelope = payload.get("atlas_trajectory")
        if not isinstance(envelope, dict):
            continue
        event_type = str(envelope.get("event_type") or "")
        if not event_type or event_type in _TIMELINE_SKIP:
            continue
        correlation = envelope.get("correlation") if isinstance(envelope, dict) else {}
        correlation = correlation if isinstance(correlation, dict) else {}
        data = as_mapping(envelope.get("data"))
        summary, severity = _summarise_event(event_type, data)
        events.append(
            {
                "local_id": f"seq-{row.get('seq')}",
                "order": int(number(row.get("seq")) or 0),
                "timestamp": row.get("ts") or envelope.get("occurred_at"),
                "timestamp_source": "trajectory transcript"
                if row.get("ts")
                else "trajectory envelope occurred_at",
                "source": "trajectory",
                "agent_id": correlation.get("worker_id"),
                "task_id": correlation.get("task_id"),
                "event_type": event_type,
                "summary": scrub(summary),
                "severity": severity,
                "evidence_ref": f"trajectory/{run_id}/transcript.jsonl#seq={row.get('seq')}",
            }
        )
    return events


def _summarise_event(event_type: str, data: dict[str, Any]) -> tuple[str, str]:
    if event_type == "action_proposed":
        return (
            f"proposed {data.get('tool_name') or data.get('tool_id')} "
            f"call {(data.get('call_id') or '')[-8:]}",
            "info",
        )
    if event_type == "action_decision":
        decision = str(data.get("decision"))
        reasons = ", ".join(str(item) for item in data.get("reason_codes") or [])
        return (
            f"{decision} {data.get('tool_id')} ({data.get('phase')}"
            f"{'; ' + reasons if reasons else ''})",
            "deny" if decision == "DENY" else "info",
        )
    if event_type == "execution_outcome":
        return (f"execution {data.get('state')} for {data.get('tool_id')}", "info")
    if event_type == "tool_observation":
        size = data.get("result_size")
        return (
            f"observation {'error' if data.get('is_error') else 'ok'} "
            f"{data.get('outcome') or ''} {size if size is not None else '?'} bytes",
            "error" if data.get("is_error") else "info",
        )
    if event_type == "sandbox_lifecycle":
        return (
            f"sandbox {data.get('state')} via {data.get('provider_name')}",
            "info",
        )
    if event_type == "worker_lifecycle":
        detail = data.get("stop_code") or data.get("stop_reason") or ""
        return (
            f"worker {data.get('mode')} {data.get('worker_id')} {detail}".strip(),
            "warn" if data.get("mode") == "stop" and detail else "info",
        )
    if event_type == "run_terminal":
        return (f"run terminal: {data.get('status')}", "info")
    if event_type == "tool_disclosed":
        return (f"tool disclosed: {data.get('tool_name')}", "info")
    if event_type in {"suspension_created", "approval_resolved", "resume_started"}:
        return (f"{event_type}: {data.get('decision') or data.get('resume_point') or ''}", "warn")
    return (event_type, "info")


def _delegation_events(model: RunModel) -> list[dict[str, Any]]:
    session = model.session
    if not session:
        return []
    delegation = _nested(session, "delegation") or {}
    origin = parse_time(delegation.get("started_at"))
    events: list[dict[str, Any]] = []
    for batch in delegation.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        round_id = batch.get("round")
        decision = as_mapping(batch.get("decision"))
        route = batch.get("route") or decision.get("route")
        events.append(
            {
                "local_id": f"delegate-round-{round_id}",
                "order": 10_000 + int(number(round_id) or 0) * 100,
                "timestamp": iso(origin),
                "timestamp_source": "delegation.started_at",
                "source": "session.delegation",
                "agent_id": _nested(session, "main", "worker_id")
                or _nested(session, "main", "agent"),
                "task_id": None,
                "event_type": "delegation_admitted",
                "summary": scrub(
                    f"router admitted round {round_id}: {route} "
                    f"({', '.join(decision.get('reason_codes') or []) or 'no reason codes'})"
                ),
                "severity": "info",
                "evidence_ref": "session.json#delegation.batches",
            }
        )
        span = number(batch.get("batch_span_ms"))
        if origin is not None and span is not None:
            events.append(
                {
                    "local_id": f"delegate-round-{round_id}-end",
                    "order": 10_000 + int(number(round_id) or 0) * 100 + 1,
                    "timestamp": iso(origin + timedelta(milliseconds=float(span))),
                    "timestamp_source": "delegation.started_at + batch_span_ms",
                    "source": "session.delegation",
                    "agent_id": _nested(session, "main", "worker_id")
                    or _nested(session, "main", "agent"),
                    "task_id": None,
                    "event_type": "delegation_finished",
                    "summary": scrub(
                        f"round {round_id} finished, max observed concurrency "
                        f"{batch.get('max_observed_concurrency')}"
                    ),
                    "severity": "info",
                    "evidence_ref": "session.json#delegation.batches",
                }
            )
        for child in batch.get("children") or []:
            if not isinstance(child, dict):
                continue
            worker_id = child.get("worker_id") or child.get("key")
            for label, offset_key, suffix in (
                ("started", "started_offset_ms", "start"),
                ("finished", "finished_offset_ms", "finish"),
            ):
                moment = _offset_time(origin, child.get(offset_key))
                events.append(
                    {
                        "local_id": f"{worker_id}-{suffix}",
                        "order": 10_000
                        + int(number(round_id) or 0) * 100
                        + int(number(child.get(offset_key)) or 0) // 1000,
                        "timestamp": iso(moment),
                        "timestamp_source": f"delegation.started_at + {offset_key}",
                        "source": "session.delegation",
                        "agent_id": worker_id,
                        "task_id": child.get("key"),
                        "event_type": f"worker_{label}",
                        "summary": scrub(
                            f"worker {child.get('key')} {label} "
                            f"({child.get('profile_id')}, {child.get('status')})"
                        ),
                        "severity": "info",
                        "evidence_ref": "session.json#delegation.batches.children",
                    }
                )
    return events


def _cell_lifecycle_events(model: RunModel) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    attempt = model.attempt
    if attempt.get("claimed_at"):
        events.append(
            {
                "local_id": "attempt-claimed",
                "order": -100,
                "timestamp": attempt.get("claimed_at"),
                "timestamp_source": "attempt.json claimed_at",
                "source": "attempt",
                "agent_id": None,
                "task_id": attempt.get("cell_id"),
                "event_type": "cell_claimed",
                "summary": scrub(
                    f"cell attempt {attempt.get('state')} (limit {attempt.get('attempt_limit')})"
                ),
                "severity": "info",
                "evidence_ref": "attempt.json",
            }
        )
    for role, field_name in (("dispatch", "dispatched_at"), ("ledger", "recorded_at")):
        relative = model.run.first(role)
        if relative is None:
            continue
        try:
            rows = model.reader.read_jsonl(relative)
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            EvidenceTooLarge,
            PathOutsideRoot,
        ):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not row.get(field_name):
                continue
            verdict = row.get("verdict")
            tokens = row.get("cumulative_tokens")
            summary = (
                f"dispatched cell order {row.get('order')}"
                if role == "dispatch"
                else f"ledger recorded {verdict} at {tokens} tokens"
            )
            events.append(
                {
                    "local_id": f"{role}-{index}",
                    "order": -50 + index,
                    "timestamp": row.get(field_name),
                    "timestamp_source": f"{role}.jsonl {field_name}",
                    "source": role,
                    "agent_id": None,
                    "task_id": row.get("cell_id"),
                    "event_type": f"cell_{role}",
                    "summary": scrub(summary),
                    "severity": "info",
                    "evidence_ref": f"{role}.jsonl#{index}",
                }
            )
    return events


def _native_events(model: RunModel) -> list[dict[str, Any]]:
    native = model.native
    if not native:
        return []
    events: list[dict[str, Any]] = []
    for local_id, key, label in (
        ("-start", "started_at", "native run started"),
        ("-end", "finished_at", "native run finished"),
    ):
        if native.get(key):
            events.append(
                {
                    "local_id": f"native{local_id}",
                    "order": 5_000,
                    "timestamp": native.get(key),
                    "timestamp_source": f"native result {key}",
                    "source": "native_result",
                    "agent_id": next(iter(native.get("worker_results") or {}), None),
                    "task_id": _nested(native, "identity", "sample_id"),
                    "event_type": (
                        "native_run_started" if local_id == "-start" else "native_run_finished"
                    ),
                    "summary": scrub(f"{label} ({native.get('mode')})"),
                    "severity": "info",
                    "evidence_ref": f"result.json#{key}",
                }
            )
    return events


def _app_events(model: RunModel) -> list[dict[str, Any]]:
    relative = model.run.first("app_record")
    if relative is None:
        return []
    events_relative = relative.rsplit("/", 1)[0] + "/events.jsonl"
    try:
        rows = model.reader.read_jsonl(events_relative)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, EvidenceTooLarge, PathOutsideRoot):
        return []
    return [
        {
            "local_id": f"app-{row.get('id')}",
            "order": int(number(row.get("id")) or 0),
            "timestamp": row.get("timestamp"),
            "timestamp_source": "events.jsonl timestamp",
            "source": "app_events",
            "agent_id": None,
            "task_id": None,
            "event_type": str(row.get("type") or "event"),
            "summary": scrub(json.dumps(row.get("data") or {}, sort_keys=True)),
            "severity": "info",
            "evidence_ref": f"{events_relative}#{row.get('id')}",
        }
        for row in rows
        if isinstance(row, dict)
    ]


def _verification_events(model: RunModel) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    axes = _verification_axes(model)
    session = model.session
    public_runs = _nested(session, "public_tests", "runs") or []
    for index, run in enumerate(public_runs):
        if not isinstance(run, dict):
            continue
        events.append(
            {
                "local_id": f"public-test-{index}",
                "order": 20_000 + index,
                "timestamp": None,
                "timestamp_source": None,
                "source": "session.public_tests",
                "agent_id": _nested(session, "main", "agent"),
                "task_id": None,
                "event_type": "public_test",
                "summary": scrub(
                    f"public test call {run.get('call_index')}: {run.get('status')} "
                    f"exit={run.get('exit_code')}"
                ),
                "severity": "ok" if run.get("status") == "PASS" else "error",
                "evidence_ref": "session.json#public_tests.runs",
            }
        )
    if known(axes.get("independent_oracle")):
        events.append(
            {
                "local_id": "oracle",
                "order": 20_500,
                "timestamp": None,
                "timestamp_source": None,
                "source": "verification",
                "agent_id": None,
                "task_id": None,
                "event_type": "oracle_verdict",
                "summary": scrub(f"independent oracle: {axes['independent_oracle']}"),
                "severity": "ok" if axes["independent_oracle"] == "PASS" else "error",
                "evidence_ref": "verification axes",
            }
        )
    if known(axes.get("execution_integrity")):
        events.append(
            {
                "local_id": "integrity",
                "order": 20_600,
                "timestamp": None,
                "timestamp_source": None,
                "source": "verification",
                "agent_id": None,
                "task_id": None,
                "event_type": "execution_integrity",
                "summary": scrub(f"execution integrity: {axes['execution_integrity']}"),
                "severity": "ok" if axes["execution_integrity"] == "PASS" else "error",
                "evidence_ref": "verification axes",
            }
        )
    return events


# ------------------------------------------------------------------ delegation


def delegation(model: RunModel) -> dict[str, Any]:
    session = model.session
    evidence = model.evidence
    if not session and not evidence:
        return {
            "run_key": model.run.run_key,
            "state": NOT_APPLICABLE,
            "reason": "this source kind has no delegation plane",
            "delegation_enabled": NOT_APPLICABLE,
            "router": None,
            "proposals": [],
            "batches": [],
            "children": [],
            "handoffs": [],
            "refusals": [],
        }
    delegation_doc = _nested(session, "delegation") or {}
    batches = [batch for batch in delegation_doc.get("batches") or [] if isinstance(batch, dict)]
    enabled = session.get("delegation_enabled")
    tool_calls = _nested(session, "main", "tool_calls") or []
    proposals = []
    children = []
    refusals = []
    for batch in batches:
        decision = as_mapping(batch.get("decision"))
        frozen = as_mapping(decision.get("frozen_decision"))
        proposals.append(
            {
                "round": batch.get("round"),
                "route": batch.get("route") or decision.get("route"),
                "proposal_id": frozen.get("proposal_id"),
                "approved_task_ids": list(frozen.get("approved_task_ids") or []),
                "reason_codes": list(decision.get("reason_codes") or []),
                "blocked_gates": list(decision.get("blocked_gates") or []),
                "policy_version": decision.get("policy_version"),
                "policy_digest": decision.get("policy_digest"),
                "max_wave_width": decision.get("max_wave_width"),
                "evidence_refs": list(frozen.get("evidence_refs") or []),
                "gates": [
                    {
                        "gate": gate.get("gate"),
                        "status": gate.get("status"),
                        "reason_codes": list(gate.get("reason_codes") or []),
                    }
                    for gate in frozen.get("gates") or []
                    if isinstance(gate, dict)
                ],
            }
        )
        for child in decision.get("children") or []:
            if isinstance(child, dict) and child.get("admitted") is False:
                refusals.append(
                    {
                        "task": child.get("key"),
                        "profile_id": child.get("profile_id"),
                        "reason_codes": list(child.get("reason_codes") or []),
                        "source": "router decision",
                    }
                )
        for child in batch.get("children") or []:
            if not isinstance(child, dict):
                continue
            children.append(
                {
                    "task": child.get("key"),
                    "worker_id": child.get("worker_id"),
                    "profile_id": child.get("profile_id"),
                    "status": child.get("status"),
                    "success": child.get("success"),
                    "stop_reason": child.get("stop_reason"),
                    "turns": child.get("turns"),
                    "tool_calls": child.get("tool_calls"),
                    "started_offset_ms": child.get("started_offset_ms"),
                    "finished_offset_ms": child.get("finished_offset_ms"),
                    "prompt_tokens": child.get("total_prompt_tokens"),
                    "completion_tokens": child.get("completion_tokens"),
                    "handoff_ref": child.get("ref_id"),
                    "error_code": child.get("error_code"),
                }
            )

    observed_tools = [str(name) for name in tool_calls]
    proposal_called = "delegate_tasks" in observed_tools
    if not session:
        state = NOT_APPLICABLE
        reason = "the cell evidence records no delegation plane"
    elif enabled is True and batches:
        state = "DELEGATION_OBSERVED"
        reason = f"{len(batches)} admitted round(s) recorded"
    elif enabled is True and not batches:
        state = NOT_OBSERVED
        reason = "the delegation plane was enabled but no admitted batch was recorded; " + (
            "the main agent did call delegate_tasks"
            if proposal_called
            else "the main agent never called delegate_tasks"
        )
    elif enabled is False:
        state = NOT_CONFIGURED
        reason = "delegation was disabled for this run"
    else:
        state = NOT_OBSERVED
        reason = "the session does not record whether delegation was enabled"

    overlaps = _delegation_overlap(children, delegation_doc)
    handoffs = _handoff_rows(model)
    metrics = _nested(session, "metrics") or {}
    return {
        "run_key": model.run.run_key,
        "state": state,
        "reason": reason,
        "delegation_enabled": enabled if enabled is not None else UNKNOWN,
        "delegate_tasks_called": proposal_called if session else NOT_APPLICABLE,
        "limits": delegation_doc.get("limits") or {},
        "policy": {
            "version": _first_known(
                (proposals[0] if proposals else {}).get("policy_version"), UNAVAILABLE
            ),
            "digest": _first_known(
                (proposals[0] if proposals else {}).get("policy_digest"), UNAVAILABLE
            ),
        },
        "router": {
            "decision_observed": bool(proposals),
            "route": _first_known(
                (proposals[0] if proposals else {}).get("route"),
                UNKNOWN if proposals else NOT_OBSERVED,
            ),
            "reason_codes": (proposals[0] if proposals else {}).get("reason_codes") or [],
            "blocked_gates": (proposals[0] if proposals else {}).get("blocked_gates") or [],
            "state": NOT_OBSERVED if not proposals else "OBSERVED",
        },
        "proposals": proposals,
        "batches": [
            {
                "round": batch.get("round"),
                "route": batch.get("route"),
                "span_ms": batch.get("batch_span_ms"),
                "max_observed_concurrency": batch.get("max_observed_concurrency"),
                "totals": batch.get("totals") or {},
            }
            for batch in batches
        ],
        "children": children,
        "concurrency": {
            "max_observed": _first_known(
                metrics.get("max_observed_child_concurrency"),
                (batches[0].get("max_observed_concurrency") if batches else None),
            ),
            "basis": "recorded child offsets, not a planned DAG",
            "overlapping_pairs": overlaps,
        },
        "handoffs": handoffs,
        "refusals": refusals,
        "registry": _nested(session, "delegation_controls", "profiles") or {},
        "exposure": _nested(session, "delegation_controls", "exposure") or {},
    }


def _delegation_overlap(
    children: list[dict[str, Any]], delegation_doc: dict[str, Any]
) -> list[dict[str, Any]]:
    windows = [
        (
            str(child.get("task")),
            number(child.get("started_offset_ms")),
            number(child.get("finished_offset_ms")),
        )
        for child in children
    ]
    pairs = []
    for index, (name_a, start_a, end_a) in enumerate(windows):
        for name_b, start_b, end_b in windows[index + 1 :]:
            if start_a is None or end_a is None or start_b is None or end_b is None:
                continue
            overlap = min(float(end_a), float(end_b)) - max(float(start_a), float(start_b))
            if overlap > 0:
                pairs.append(
                    {
                        "left": name_a,
                        "right": name_b,
                        "overlap_ms": round(overlap, 3),
                        "basis": "child started/finished offsets inside the round",
                    }
                )
    return pairs


def _handoff_rows(model: RunModel) -> list[dict[str, Any]]:
    index = model.handoff_index
    refs = as_mapping(index.get("refs"))
    rows: list[dict[str, Any]] = []
    for ref_id, meta in refs.items():
        if not isinstance(meta, dict):
            continue
        rows.append(
            {
                "ref_id": ref_id,
                "task": meta.get("key"),
                "worker_id": meta.get("worker_id"),
                "parent_agent": meta.get("parent_agent"),
                "parent_task_id": meta.get("parent_task_id"),
                "source": "delegation/handoffs.json",
            }
        )
    for path in model.run.files.get("handoff_record") or []:
        try:
            value = model.reader.read_json(path)
        except (
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            EvidenceTooLarge,
            PathOutsideRoot,
        ):
            continue
        if not isinstance(value, dict):
            continue
        ref_id = value.get("ref_id")
        for row in rows:
            if row["ref_id"] == ref_id:
                row["created_at"] = value.get("created_at")
                row["length"] = value.get("length")
                row["content_digest"] = value.get("digest")
                row["producer_title"] = value.get("producer_title")
    return rows


# ------------------------------------------------------------------- resources


def resources(model: RunModel) -> dict[str, Any]:
    session = model.session
    evidence = model.evidence
    native = model.native
    metrics = _nested(session, "metrics") or {}
    usage = _usage_block(model)
    analysis_outcome = _nested(model.analysis, "outcome") or {}

    series: list[dict[str, Any]] = []
    series_source = UNAVAILABLE
    per_request = metrics.get("main_context_tokens_by_request")
    if isinstance(per_request, list) and per_request:
        series = [
            {"request_index": index + 1, "input_tokens": value}
            for index, value in enumerate(per_request)
            if number(value) is not None
        ]
        series_source = "session.metrics.main_context_tokens_by_request"
    evidence_series = _nested(evidence, "context", "per_request_input_tokens")
    if not series and isinstance(evidence_series, list) and evidence_series:
        series = [
            {"request_index": index + 1, "input_tokens": value}
            for index, value in enumerate(evidence_series)
            if number(value) is not None
        ]
        series_source = "cell evidence per_request_input_tokens"
    samples = _nested(native, "metrics", "budget", "samples")
    if not series and isinstance(samples, list) and samples:
        series = [
            {
                "request_index": number(item.get("request_id")),
                "input_tokens": number(item.get("actual_prompt_tokens")),
                "completion_tokens": number(item.get("actual_completion_tokens")),
            }
            for item in samples
            if isinstance(item, dict)
        ]
        series_source = "native budget samples"

    budget = _nested(session, "budget") or (
        _nested(evidence, "usage", "budget") or _nested(native, "metrics", "budget") or {}
    )
    peak_main = _first_known(
        metrics.get("main_peak_context_tokens"),
        _nested(evidence, "context", "main_peak_context"),
        _nested(evidence, "context", "primary_agent_peak_context"),
    )
    agent_count = len(agent_views(model))
    recorded_system_peak = _nested(evidence, "context", "system_max_agent_context")
    if known(recorded_system_peak):
        system_peak: Any = recorded_system_peak
    elif agent_count == 1 and series:
        # With exactly one agent, the per-request observation is that agent's peak, so the
        # system maximum equals it. With several agents the main series cannot stand in for it.
        system_peak = _series_peak(series)
    else:
        system_peak = UNKNOWN
    census = _nested(evidence, "context", "agent_context_census") or {}

    return {
        "run_key": model.run.run_key,
        "tokens": {
            **usage,
            "peak_context": {
                "main": peak_main,
                "system_max_agent": system_peak,
                "census_complete": census.get("complete", NOT_APPLICABLE),
                "basis": (
                    census.get("basis")
                    if known(recorded_system_peak)
                    else (
                        "single agent: the per-request peak is that agent's maximum"
                        if agent_count == 1 and series
                        else "no agent context census exists for this run"
                    )
                ),
                "agents_observed": agent_count,
            },
            "delta_cost_usd": _first_known(analysis_outcome.get("delta_cost_usd")),
        },
        "attribution": {
            "main": usage.get("main_prompt_tokens"),
            "children": usage.get("child_prompt_tokens"),
            "system_total": usage.get("system_total_tokens"),
            "known_floor": usage.get("known_floor_tokens"),
            "note": (
                "a known floor only sums the components that were reported; "
                "it is never presented as a system total"
            ),
        },
        "request_series": {
            "source": series_source,
            "points": series,
            "peak": _series_peak(series),
            "cumulative": _sum_series(series),
        },
        "budget": {
            "mode": _first_known(budget.get("mode"), budget.get("enforcement"), NOT_CONFIGURED),
            "limit_tokens": budget.get("limit_tokens", UNKNOWN),
            "used_tokens": budget.get("used_tokens", UNKNOWN),
            "remaining_tokens": budget.get("remaining_tokens", UNKNOWN),
            "requests_started": budget.get("requests_started", UNKNOWN),
            "requests_completed": budget.get("requests_completed", UNKNOWN),
            "admission_rejections": budget.get("admission_rejections", UNKNOWN),
            "stop_reason": budget.get("stop_reason"),
            "written_off_tokens": budget.get("written_off_tokens", UNKNOWN),
            "enforcement": budget.get("enforcement", UNAVAILABLE),
        },
        "cost": _cost(model, model.run.matrix_row or {}),
        "cache": {
            "hit": usage.get("cache_hit_tokens"),
            "miss": usage.get("cache_miss_tokens"),
            "hit_rate": _hit_rate(usage),
            "note": "a cache hit is an optimisation signal, never correctness state",
        },
    }


def _sum_series(series: list[dict[str, Any]]) -> Any:
    values = [number(item.get("input_tokens")) for item in series]
    real = [value for value in values if value is not None]
    return sum(real) if real else UNAVAILABLE


def _series_peak(series: list[dict[str, Any]]) -> Any:
    values = [number(item.get("input_tokens")) for item in series]
    real = [value for value in values if value is not None]
    return max(real) if real else UNAVAILABLE


def _hit_rate(usage: dict[str, Any]) -> Any:
    hit = number(usage.get("cache_hit_tokens"))
    miss = number(usage.get("cache_miss_tokens"))
    if hit is None or miss is None or hit + miss == 0:
        return UNKNOWN
    return round(hit / (hit + miss) * 100, 2)


# ----------------------------------------------------------------------- tools


def tools(model: RunModel) -> dict[str, Any]:
    session = model.session
    rows: dict[str, dict[str, Any]] = {}
    for row in model.transcript:
        payload = row.get("payload")
        envelope = payload.get("atlas_trajectory") if isinstance(payload, dict) else None
        if not isinstance(envelope, dict):
            continue
        event_type = str(envelope.get("event_type") or "")
        data = as_mapping(envelope.get("data"))
        correlation = envelope.get("correlation")
        correlation = correlation if isinstance(correlation, dict) else {}
        call_id = data.get("call_id") or correlation.get("call_id")
        if not call_id:
            continue
        entry = rows.setdefault(
            str(call_id),
            {
                "call_id": str(call_id),
                "agent_id": correlation.get("worker_id"),
                "tool": data.get("tool_name") or data.get("tool_id") or correlation.get("tool_id"),
                "decision": UNKNOWN,
                "reason_codes": [],
                "phase": None,
                "outcome": UNKNOWN,
                "is_error": UNKNOWN,
                "result_size": UNKNOWN,
                "timestamp": row.get("ts"),
                "source": "trajectory",
                "input_withheld": True,
                "output_withheld": True,
                "input_excerpt": None,
            },
        )
        if event_type == "action_decision":
            entry["decision"] = data.get("decision")
            entry["reason_codes"] = list(data.get("reason_codes") or [])
            entry["phase"] = data.get("phase")
            entry["timestamp"] = row.get("ts") or entry["timestamp"]
        elif event_type == "tool_observation":
            entry["outcome"] = data.get("outcome") or ("ERROR" if data.get("is_error") else "OK")
            entry["is_error"] = data.get("is_error")
            entry["result_size"] = data.get("result_size")
        elif event_type == "execution_outcome":
            entry["outcome"] = data.get("state")
        elif event_type == "action_proposed":
            entry["tool"] = entry["tool"] or data.get("tool_name")

    for index, call in enumerate(_nested(session, "main", "tool_calls") or []):
        synthetic = f"session-main-tool-{index}"
        if any(row["tool"] == call for row in rows.values()):
            continue
        rows[synthetic] = {
            "call_id": None,
            "agent_id": _nested(session, "main", "agent"),
            "tool": call,
            "decision": UNKNOWN,
            "reason_codes": [],
            "phase": None,
            "outcome": UNKNOWN,
            "is_error": UNKNOWN,
            "result_size": UNKNOWN,
            "timestamp": None,
            "source": "session.main.tool_calls (names only)",
            "input_withheld": True,
            "output_withheld": True,
            "input_excerpt": None,
        }

    for denial in _nested(session, "ownership", "denials") or []:
        if not isinstance(denial, dict):
            continue
        rows[f"denial-{len(rows)}"] = {
            "call_id": None,
            "agent_id": denial.get("worker_id"),
            "tool": denial.get("tool_name"),
            "decision": denial.get("decision"),
            "reason_codes": [denial.get("reason_code")],
            "phase": "execution",
            "outcome": "DENIED",
            "is_error": True,
            "result_size": 0,
            "timestamp": None,
            "source": "session.ownership.denials",
            # The denied command is tool input: it stays out of the browser. The refusal reason
            # is what explains the decision.
            "input_withheld": True,
            "output_withheld": True,
            "input_excerpt": clip(denial.get("detail"), 200),
            "detail": clip(denial.get("detail"), 200),
        }

    for index, run in enumerate(_nested(session, "public_tests", "runs") or []):
        if not isinstance(run, dict):
            continue
        rows[f"public-test-{index}"] = {
            "call_id": None,
            "agent_id": _nested(session, "main", "agent"),
            "tool": "run_public_tests",
            "decision": "ALLOW",
            "reason_codes": [],
            "phase": "verification",
            "outcome": run.get("status"),
            "is_error": run.get("status") != "PASS",
            "result_size": run.get("output_chars"),
            "timestamp": None,
            "source": "session.public_tests.runs",
            "input_withheld": True,
            "output_withheld": True,
            "input_excerpt": clip(run.get("reason"), 200),
        }

    ordered = sorted(
        rows.values(),
        key=lambda item: (
            item["timestamp"] is None,
            item["timestamp"] or "",
            str(item["tool"]),
            str(item["call_id"]),
        ),
    )
    declared = _nested(session, "tool_surface")
    return {
        "run_key": model.run.run_key,
        "state": "TRANSCRIPT_DERIVED"
        if model.transcript
        else ("SESSION_NAMES_ONLY" if session else UNAVAILABLE),
        "declared_surface": declared or UNAVAILABLE,
        "total": len(ordered),
        "rows": ordered,
        "notes": [
            "tool inputs and outputs stay in the private payload corpus; only sizes, "
            "decisions and reason codes are projected",
            "a call with decision UNKNOWN has no recorded M3 decision in the available evidence",
        ],
    }


# ------------------------------------------------------------------- artifacts


def artifacts(model: RunModel) -> dict[str, Any]:
    patch_files = model.read_patch_files()
    session = model.session
    evidence = model.evidence
    export = _nested(evidence, "export") or {}
    session_artifacts = _nested(session, "artifacts") or {}
    native = model.native

    files: list[dict[str, Any]] = []
    for path in patch_files:
        files.append({"path": path, "source": "workspace.patch"})
    workspace_changes = _nested(evidence, "verification", "workspace_changes") or {}
    for path in workspace_changes.get("changed") or []:
        if not any(item["path"] == path for item in files):
            files.append({"path": path, "source": "cell evidence workspace_changes"})

    handoffs = _handoff_rows(model)
    return {
        "run_key": model.run.run_key,
        "patch": {
            "available": model.run.has("patch"),
            "path": model.run.first("patch"),
            "changed_files": files,
            "changed_count": len(files),
            "digest": _first_known(
                export.get("patch_digest"),
                _nested(session, "sandbox", "patch_digest"),
            ),
            "recorded_changed_files": _first_known(
                export.get("patch_changed_files"),
                _nested(session, "sandbox", "patch_changed_files"),
                len(patch_files) if patch_files else None,
            ),
        },
        "workspace": {
            "export_path": _first_known(
                export.get("workspace_path"),
                session_artifacts.get("workspace_path"),
                native.get("workspace_export"),
            ),
            "files": _first_known(
                export.get("workspace_files"),
                session_artifacts.get("workspace_files"),
            ),
            "digest": _first_known(
                export.get("workspace_digest"), session_artifacts.get("workspace_digest")
            ),
            "directory_digest": _first_known(export.get("directory_digest")),
            "note": "the export is referenced, never copied into the API response",
        },
        "handoffs": handoffs,
        "deliverable": {
            "oracle_mode": _first_known(
                _nested(session, "verification", "oracle_mode"),
                _nested(evidence, "verification", "oracle_mode"),
            ),
            "required_artifacts": _nested(model.native, "oracle", "required_artifacts")
            or UNAVAILABLE,
        },
        "notes": [
            "artifact contents are not served; only references, digests and file lists",
        ],
    }


# ---------------------------------------------------------------- verification


def verification(model: RunModel) -> dict[str, Any]:
    axes = _verification_axes(model)
    session = model.session
    evidence = model.evidence
    analysis = model.analysis
    public_runs = _nested(session, "public_tests", "runs") or []
    checks = _nested(evidence, "verification", "checks") or []
    return {
        "run_key": model.run.run_key,
        "headline": _headline(axes, executed=_has_execution_evidence(model)),
        "axes": axes,
        "detail": {
            "agent_success_source": axes["axis_sources"]["raw_agent_success"],
            "effective_completion": axes["effective_agent_completion"],
            "effective_completion_source": axes["axis_sources"]["effective_agent_completion"],
            "public_tests": [
                {
                    "call_index": run.get("call_index"),
                    "status": run.get("status"),
                    "exit_code": run.get("exit_code"),
                    "duration_ms": run.get("duration_ms"),
                    "output_chars": run.get("output_chars"),
                    "reason": clip(run.get("reason"), 200),
                }
                for run in public_runs
                if isinstance(run, dict)
            ],
            "static_acceptance": _nested(session, "verification", "static_status"),
            "oracle": _oracle_detail(model),
            "integrity_checks": [
                {
                    "check": item.get("check"),
                    "status": item.get("status"),
                    "detail": clip(item.get("detail"), 200),
                    "affects_verdict": item.get("affects_verdict"),
                }
                for item in checks
                if isinstance(item, dict)
            ],
            "executor_checks": [
                {
                    "check": item.get("check"),
                    "ok": item.get("ok"),
                    "detail": clip(item.get("detail"), 200),
                }
                for item in (_nested(analysis, "outcome", "checks") or [])
                if isinstance(item, dict)
            ],
            "stops": _nested(analysis, "outcome", "stops") or [],
        },
        "note": (
            "raw agent success, effective completion, the public suite, the independent "
            "oracle and execution integrity stay separate; no single badge replaces them"
        ),
    }


def _oracle_detail(model: RunModel) -> dict[str, Any]:
    analysis_oracle = model.analysis.get("oracle")
    native_oracle = model.native.get("oracle")
    if isinstance(analysis_oracle, dict):
        return {
            "status": analysis_oracle.get("status"),
            "kind": analysis_oracle.get("kind"),
            "passed": analysis_oracle.get("passed"),
            "execution_mode": analysis_oracle.get("execution_mode"),
            "environment_failure": analysis_oracle.get("environment_failure"),
            "returncode": analysis_oracle.get("returncode"),
            "image": analysis_oracle.get("image"),
            "source": analysis_oracle.get("source"),
            "diagnostic": clip(analysis_oracle.get("stderr_excerpt"), 300),
            "evidence": "cell analysis oracle block",
        }
    if isinstance(native_oracle, dict):
        return {
            "status": "PASS" if native_oracle.get("passed") else "FAIL",
            "kind": native_oracle.get("failure_kind"),
            "passed": native_oracle.get("passed"),
            "execution_mode": native_oracle.get("execution_mode"),
            "environment_failure": native_oracle.get("environment_failure"),
            "diagnostic": clip(
                " ".join(str(item) for item in native_oracle.get("failure_reasons") or []), 300
            ),
            "evidence": "native result oracle block",
        }
    return {"status": UNKNOWN, "evidence": UNAVAILABLE}


# -------------------------------------------------------------------- requests


def requests_view(model: RunModel) -> dict[str, Any]:
    rows, metadata, journal_source = _request_journal(model)
    if rows is None:
        return {
            "run_key": model.run.run_key,
            "state": UNAVAILABLE,
            "reason": _request_reason(model),
            "journal": {"source": journal_source, "available": False},
            "counts": {},
            "entries": [],
            "note": (
                "no per-request lifecycle journal exists for this run; the workbench will not "
                "invent an error type, an HTTP status or a usage number"
            ),
        }
    entries = []
    for index, row in enumerate(rows):
        usage = {
            key: value
            for key, value in as_mapping(row.get("usage")).items()
            if key in _REQUEST_USAGE_COUNTERS and _request_count(value) is not None
        }
        reported = row.get("usage_reported")
        usage_status = row.get("usage_status") or ("COMPLETE" if usage else UNKNOWN)
        if reported is False:
            usage = {}
            usage_status = UNKNOWN
        agent = (
            row.get("agent")
            if "agent" in row
            else (row.get("agent_id") or row.get("worker_id") or row.get("worker"))
        )
        entries.append(
            {
                "request_id": clip(row.get("request_id") or row.get("id"), 120) or index + 1,
                "agent_id": clip(agent, 120),
                "provider": clip(row.get("provider") or metadata.get("provider"), 120) or UNKNOWN,
                "phase": clip(row.get("phase"), 80) or UNKNOWN,
                "state": _request_state(row),
                "requested_model": clip(
                    row.get("requested_model")
                    or row.get("model")
                    or metadata.get("requested_model"),
                    120,
                )
                or UNKNOWN,
                "returned_model": clip(row.get("returned_model"), 120) or UNKNOWN,
                "http_status": _request_count(row.get("http_status") or row.get("status_code"))
                or UNKNOWN,
                "error_type": clip(
                    row.get("exception_type") or row.get("error_type") or row.get("error_class"),
                    120,
                )
                or UNKNOWN,
                "provider_error_code": clip(row.get("provider_error_code"), 120),
                "error_message": clip(
                    row.get("message") or row.get("error_message") or row.get("error")
                ),
                "partial_response": row.get("partial_response")
                if isinstance(row.get("partial_response"), bool)
                else None,
                "usage_reported": reported
                if isinstance(reported, bool)
                else (True if usage else None),
                "usage": usage or None,
                "usage_status": clip(usage_status, 80),
                "started_at": clip(row.get("started_at"), 100),
                "finished_at": clip(row.get("finished_at"), 100),
            }
        )
    states = [entry["state"] for entry in entries]
    truncated = metadata.get("records_truncated") is True
    counts = as_mapping(metadata.get("counts"))
    state_fields = {
        "started": "STARTED",
        "completed": "COMPLETED",
        "failed": "FAILED",
        "cancelled": "CANCELLED",
        "unknown_terminal": "UNKNOWN_TERMINAL_STATE",
    }
    full_total = _request_count(metadata.get("requests_total")) if truncated else len(entries)
    return {
        "run_key": model.run.run_key,
        "state": "AVAILABLE",
        "journal": {"source": journal_source, "available": True, "records_truncated": truncated},
        "counts": {
            "total": full_total,
            "retained": len(entries),
            **{
                label: _request_count(counts.get(state)) if truncated else states.count(state)
                for label, state in state_fields.items()
            },
        },
        "entries": entries,
        "note": (
            "The producer truncated the journal. Counts cover the full run when recorded."
            if truncated
            else "Lifecycle states are copied from the journal."
        ),
    }


_REQUEST_USAGE_COUNTERS = frozenset(
    {
        "prompt_tokens",
        "completion_tokens",
        "input_tokens",
        "output_tokens",
        "cache_hit_tokens",
        "cache_miss_tokens",
        "cache_read_input_tokens",
    }
)


def _request_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _request_state(row: dict[str, Any]) -> str:
    value = str(row.get("state") or row.get("status") or "").upper()
    if value in {"STARTED", "COMPLETED", "FAILED", "CANCELLED"}:
        return value
    return "UNKNOWN_TERMINAL_STATE"


def _request_reason(model: RunModel) -> str:
    if model.session or model.analysis or model.evidence:
        return "No recognized provider request journal was found in the available evidence."
    return "this source kind records no provider request lifecycle"


# ---------------------------------------------------------------------- overview


def overview(model: RunModel) -> dict[str, Any]:
    summary_payload = summary(model)
    agents = agent_views(model)
    axes = _verification_axes(model)
    topology = _topology(model, agents)
    return {
        **summary_payload,
        "goal": _goal_text(model),
        "topology": topology,
        "agent_roster": [
            {
                "agent_id": item["agent_id"],
                "role": item["role"],
                "profile_id": item["profile_id"],
                "stop_reason": item["stop_reason"],
                "tool_calls": item.get("tool_calls"),
            }
            for item in agents
        ],
        "verification_summary": {
            "headline": _headline(axes, executed=_has_execution_evidence(model)),
            "axes": axes,
        },
        "available_views": _available_views(model),
    }


def _goal_text(model: RunModel) -> str:
    for value in (
        model.session.get("goal"),
        _nested(model.native, "task"),
        _nested(model.app, "task"),
        _nested(model.atlas, "final_output"),
    ):
        if known(value):
            text = clip(value, 1200)
            if text:
                return text
    return UNAVAILABLE


def _topology(model: RunModel, agents: list[dict[str, Any]]) -> dict[str, Any]:
    planning = "PLANNED" if agents else "EMPTY"
    executed = (
        "EXECUTED"
        if any(item.get("started_at") or item.get("tool_calls") for item in agents)
        else "NOT_OBSERVED"
    )
    children = [item for item in agents if item["role"] not in {"MAIN", "NATIVE", "DAG_NODE"}]
    return {
        "agents": len(agents),
        "mains": sum(1 for item in agents if item["role"] == "MAIN"),
        "workers": len(children),
        "native_agents": sum(1 for item in agents if item["role"] == "NATIVE"),
        "dag_nodes": sum(1 for item in agents if item["role"] == "DAG_NODE"),
        "edge_source": "recorded parent links and delegation batches",
        "planned_state": planning,
        "executed_state": executed,
        "note": "planned topology and observed execution are reported separately",
    }


def _available_views(model: RunModel) -> list[str]:
    views = ["overview", "agents", "timeline", "resources", "verification", "artifacts"]
    if model.session or model.evidence:
        views.extend(["delegation", "tools"])
    if _request_rows(model) or model.session or model.analysis:
        views.append("requests")
    return sorted(set(views))
