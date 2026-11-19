"""The frozen Stage P1b contract: goal text, public suite, hidden oracle, acceptance.

One module, so the live runner and the offline verifier cannot drift apart: whatever the runner
provisions is exactly what the offline checks exercised. Everything here is Host-owned. The Main
never sees ``HIDDEN_PROGRAM``: it is not a tool, it is not written into the workspace, and the only
channel that runs it is the harness oracle after the Main stops.
"""

from __future__ import annotations

from pathlib import Path

from atlasos.app.goal_verification import GoalAcceptance
from atlasos.app.public_tests import PublicTestContract
from atlasos.verification.models import CheckKind, CheckSpec, VerificationSpec

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "stage_p1b_dual_plugin"
#: Never copied into the provisioned fixture.
FIXTURE_SKIP = {"__pycache__", ".pytest_cache", "build.py"}
FIXTURE_MANIFEST = HERE / "fixture_manifest.json"

# -- frozen live configuration ------------------------------------------------

PROVIDER = "deepseek"
MODEL = "deepseek-chat"
IMAGE = "atlas-uat-python:3.12"
TOKEN_BUDGET = 300_000
MAIN_MAX_TURNS = 20
#: The whole integration layer and nothing else: no plugin, no test, no sample is writable.
MAIN_WRITE_PATHS: tuple[str, ...] = ("core/",)
#: ``sha256`` of ``atlasos.app.main_driven.MAIN_SYSTEM_PROMPT`` as frozen by stage P0.1. The runner
#: refuses to start when the prompt on disk differs: this stage may not edit what the Main is told.
MAIN_SYSTEM_PROMPT_DIGEST = (
    "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a"
)
#: The evidence bundle the stage rules require, relative to this directory.
EVIDENCE_DIR = HERE / "evidence" / "run-1"

# -- Main-facing goal (no delegation instruction of any kind) ------------------

GOAL = (
    "The eventpipe project in this workspace ingests events from two channels: a JSON Lines file "
    "channel in plugins/file_events and a chat message channel in plugins/message_events. Its "
    "consumers need one unified normalized event whichever channel a record arrived from. Both "
    "channels already parse their own transport, but the shared path in the core package is "
    "unfinished, so tests/test_public_contract.py fails today. The two channel packages are "
    "vendored: their public functions, defaults, error codes and native record fields are a "
    "compatibility surface that other code already depends on, and the tests under tests/ are the "
    "contract. Please finish the unified path inside core so that a record from either channel "
    "normalizes to the same shape, with the existing channel behaviour and the existing suites "
    "intact. Only files inside core may be written. Use the public test suite to check your work."
)

# -- the public channel (the Main may run this) ------------------------------

PUBLIC_TESTS = PublicTestContract(
    test_id="public",
    command="python3 -B -m pytest -q tests",
    timeout_seconds=240,
    max_output_chars=6000,
    max_calls=4,
)

# -- the hidden oracle -------------------------------------------------------

#: A standalone assertion program. It is single-quoted on the shell command line, so it contains no
#: single quote, no backtick and no ``$`` anywhere. Every rule it asserts is visible to the Main in
#: the channel packages or in ``core/contracts.py``; nothing here is invented at verification time.
HIDDEN_PROGRAM = """
import json
from datetime import datetime, timedelta, timezone

from core.adapter import normalize_event, supported_sources
from core.contracts import NormalizedEvent, Severity
from core.errors import UnknownSourceError
from core.registry import build_default_registry
from plugins.file_events import FileEventError, parse_document as parse_files
from plugins.message_events import parse_document as parse_messages

UTC = timezone.utc
FAILURES = []


def check(condition, label):
    if not condition:
        FAILURES.append(label)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


registry = build_default_registry()
check(registry.sources() == ("file_events", "message_events"), "registry.sources")
check(supported_sources() == ("file_events", "message_events"), "adapter.supported_sources")


def file_event(**overrides):
    raw = {
        "event_id": "ev-0123abcd",
        "ts": "2026-11-19T02:05:06Z",
        "kind": "build.started",
        "level": "info",
        "tags": "ci",
        "attrs": {"a": "1"},
    }
    raw.update(overrides)
    return raw


def message_event(**overrides):
    raw = {
        "id": 1041,
        "sent_at": 1795053907000,
        "type": "USER_JOIN",
        "priority": 4,
        "labels": ["B", "b"],
        "payload": {"n": 3, "ok": True, "skip": None, "name": "x", "nested": {"b": 1, "a": 2}},
    }
    raw.update(overrides)
    return raw


FILE_SEVERITY = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "warn": "warning",
    "err": "error",
    "fatal": "error",
    None: "info",
}
for raw_level, expected in FILE_SEVERITY.items():
    event = normalize_event("file_events", file_event(level=raw_level))
    check(isinstance(event, NormalizedEvent), "file record type")
    check(event.severity is Severity(expected), "file severity " + str(raw_level))
    check(event.source == "file_events", "file source")
    check(event.event_id == "ev-0123abcd", "file event_id kept")
    check(event.occurred_at.utcoffset() == timedelta(0), "file occurred_at utc")
    check(event.kind == "build.started", "file kind kept")

event = normalize_event("file_events", file_event(ts="2026-11-19T04:05:06+02:00"))
check(event.occurred_at == datetime(2026, 11, 19, 2, 5, 6, tzinfo=UTC), "file offset converted")

event = normalize_event("file_events", file_event(tags=" b, A ,b,, "))
check(event.tags == ("a", "b"), "file tags split, lowered, deduped, sorted")
event = normalize_event("file_events", file_event(tags=None, attrs=None))
check(event.tags == () and event.attributes == {}, "file empty optionals")
event = normalize_event("file_events", file_event(attrs={"b": "2", "a": "1"}))
check(event.attributes == {"a": "1", "b": "2"}, "file attributes copied")

MESSAGE_SEVERITY = {1: "debug", 2: "info", 3: "info", 4: "warning", 5: "error", None: "info"}
for raw_priority, expected in MESSAGE_SEVERITY.items():
    event = normalize_event("message_events", message_event(priority=raw_priority))
    check(isinstance(event, NormalizedEvent), "message record type")
    check(event.severity is Severity(expected), "message severity " + str(raw_priority))
    check(event.source == "message_events", "message source")
    check(event.event_id == "msg-1041", "message id prefixed")
    check(event.kind == "user_join", "message kind lowercased")
    check(event.occurred_at == datetime(2026, 11, 19, 2, 5, 7, tzinfo=UTC), "message timestamp")
    check(event.occurred_at.utcoffset() == timedelta(0), "message occurred_at utc")
    check(event.tags == ("b",), "message labels normalized")
    check(
        event.attributes
        == {
            "n": "3",
            "name": "x",
            "nested": json.dumps({"b": 1, "a": 2}, sort_keys=True, separators=(",", ":")),
            "ok": "true",
        },
        "message attributes rendered as strings",
    )
    check(all(isinstance(value, str) for value in event.attributes.values()), "attribute types")

event = normalize_event("message_events", message_event(payload=None, labels=[]))
check(event.attributes == {} and event.tags == (), "message empty optionals")

naive = "naive timestamp accepted"
try:
    normalize_event("file_events", file_event(ts="2026-11-19T02:05:06"))
except Exception as error:
    naive = getattr(error, "code", None)
check(naive == "naive_timestamp", "naive timestamp code")

unknown_level = "unknown level accepted"
try:
    normalize_event("file_events", file_event(level="critical"))
except Exception as error:
    unknown_level = getattr(error, "code", None)
check(unknown_level == "unknown_level", "unknown level code")

bad_priority = "out of range priority accepted"
try:
    normalize_event("message_events", message_event(priority=9))
except Exception as error:
    bad_priority = getattr(error, "code", None)
check(bad_priority == "priority_out_of_range", "priority range code")

unknown_source = "unknown channel accepted"
try:
    normalize_event("carrier_pigeon", file_event())
except UnknownSourceError:
    unknown_source = "unknown_source"
except Exception as error:
    unknown_source = getattr(error, "code", None)
check(unknown_source == "unknown_source", "unknown channel code")

raw = message_event()
before = json.dumps(raw, sort_keys=True)
normalize_event("message_events", raw)
check(json.dumps(raw, sort_keys=True) == before, "input record mutated")

events = registry.dispatch("file_events", read("samples/events.jsonl"))
check(
    [event.event_id for event in events]
    == ["ev-1a2b3c4d", "ev-5e6f7a8b", "ev-3a4b5c6d", "ev-9c0d1e2f"],
    "file dispatch order",
)
check(all(isinstance(event, NormalizedEvent) for event in events), "dispatch returns events")
check(events[0].severity is Severity.INFO, "sample first severity")
check(events[1].severity is Severity.WARNING, "sample warn severity")
check(events[2].occurred_at == datetime(2026, 11, 19, 2, 6, 0, tzinfo=UTC), "sample default ts")
check(events[3].occurred_at == datetime(2026, 11, 19, 4, 59, 59, tzinfo=UTC), "sample offset")
check(events[3].tags == (), "sample absent tags")
check(events[3].attributes == {}, "sample absent attributes")

messages = registry.dispatch("message_events", read("samples/messages.json"))
check(
    [event.event_id for event in messages] == ["msg-1041", "msg-1042", "msg-1043"],
    "message dispatch order",
)
check(messages[0].attributes == {"channel": "#general", "invited": "true"}, "sample attributes")
check(messages[1].attributes == {"attempts": "3"}, "null payload value dropped")
check(messages[2].severity is Severity.ERROR, "sample priority 5")
again = registry.dispatch("message_events", read("samples/messages.json"))
check(
    [event.as_dict() for event in again] == [event.as_dict() for event in messages],
    "dispatch deterministic",
)

record = parse_files(
    json.dumps({"event_id": "ev-0123abcd", "ts": "2026-11-19T02:05:06Z", "kind": "a"})
)[0]
check(record.level is None, "file plugin keeps absent level raw")
check(record.ts == "2026-11-19T02:05:06Z", "file plugin keeps raw ts")
check(record.as_mapping()["attrs"] == {}, "file plugin native mapping")

native_error = "no error"
try:
    parse_files(json.dumps({"event_id": "bad", "ts": "2026-11-19T02:05:06Z", "kind": "a"}))
except FileEventError as error:
    native_error = error.code
check(native_error == "bad_event_id", "file plugin error code")

message_record = parse_messages(json.dumps([{"id": 7, "sent_at": 1, "type": "USER_JOIN"}]))[0]
check(message_record.priority is None, "message plugin keeps absent priority raw")

if FAILURES:
    raise SystemExit("HIDDEN_FAIL: " + "; ".join(FAILURES))
print("HIDDEN_OK")
"""

#: ``pytest`` first (the whole suite, including both plugin regressions), then the hidden program.
#: Joined with ``&&`` so a failing suite can never be reported as an oracle pass.
ORACLE_COMMAND = f"python3 -m pytest -q && python3 -c '{HIDDEN_PROGRAM}'"


def acceptance() -> GoalAcceptance:
    spec = VerificationSpec(
        spec_id="stage-p1b-eventpipe",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=("core/adapter.py", "core/registry.py"),
        checks=(
            CheckSpec(
                check_id="adapter-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="core/adapter.py",
            ),
            CheckSpec(
                check_id="registry-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="core/registry.py",
            ),
            CheckSpec(
                check_id="adapter-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="core/adapter.py",
            ),
            CheckSpec(
                check_id="registry-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="core/registry.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(spec=spec, dynamic_command=ORACLE_COMMAND, dynamic_timeout_seconds=420)


__all__ = [
    "EVIDENCE_DIR",
    "FIXTURE_MANIFEST",
    "FIXTURE_ROOT",
    "FIXTURE_SKIP",
    "GOAL",
    "HIDDEN_PROGRAM",
    "IMAGE",
    "MAIN_MAX_TURNS",
    "MAIN_SYSTEM_PROMPT_DIGEST",
    "MAIN_WRITE_PATHS",
    "MODEL",
    "ORACLE_COMMAND",
    "PROVIDER",
    "PUBLIC_TESTS",
    "TOKEN_BUDGET",
    "acceptance",
]
