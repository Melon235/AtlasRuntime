"""Stage S1 frozen contract: Main System Prompt v2 real-model pilot.

What S1 measures
----------------
One thing: whether the shipped Main prompt (``main-v2.0-rc1``) makes a real DeepSeek Main consider
delegation at a sensible point, propose work that is genuinely independent, use what the workers
return, and still finish a correct integration — on a coding task it has never seen, with **no**
instruction anywhere telling it to delegate.

What S1 does not measure
------------------------
Any statistical effect. This is a single pilot run, one prompt, one task. Nothing here can support a
claim that v2 is better than v1; a later stage may compare them.

Frozen inputs
-------------
The prompt text is a file, and its bytes are the contract: :data:`PROMPT_V2_DIGEST` is the digest of
:data:`PROMPT_V2_PATH`. The fixture, the public suite and the hidden oracle are all frozen here, and
the hidden program never enters the sandbox as a file — the only channel that runs it is the host
oracle after the Main stops.
"""

from __future__ import annotations

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

PROMPT_V2_PATH = HERE / "prompts" / "main_v2.md"
#: The label injected into ``session.json`` and every artefact of this stage.
PROMPT_V2_VERSION = "main-v2.0-rc1"
#: ``sha256`` of the prompt **text** — the string that becomes ``AgentConfig.system_prompt``.
#:
#: Text, not file bytes, on purpose: a checkout may materialise this file with CRLF endings, and a
#: digest that changes with the line-ending convention of a working tree is not an identity. The file
#: is stored with LF endings, so on this working tree the two digests coincide; if they ever diverge,
#: the text digest is the authoritative one and the file digest is recorded separately as provenance.
PROMPT_V2_DIGEST = "sha256:2bb50b09fb92ac6b9630cdac3972704de2d60a9a79117ffac301c8adb749a323"

#: Stage P0.1's prompt, still the runtime default and unchanged by this stage.
PROMPT_V1_VERSION = "main-v1.0"
PROMPT_V1_DIGEST = "sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a"

# -- the fixture -------------------------------------------------------------

FIXTURE_ROOT = REPO_ROOT / "experiments" / "fixtures" / "stage_s1_releaseguard"
#: Never copied into the provisioned fixture.
FIXTURE_SKIP = {"__pycache__", ".pytest_cache", ".git", "build.py"}

# -- the Main-facing goal ----------------------------------------------------
#: A user's request, nothing else. No "you must delegate", no "create two workers", no hint that the
#: run is measuring anything. The only constraint it states is the one the ownership policy enforces.
GOAL = (
    "The ReleaseGuard project in this workspace already parses release metadata and inventories "
    "build artifacts, but the shared release audit workflow is incomplete. Implement the release "
    "readiness audit so consumers can evaluate a release manifest against the actual artifact "
    "directory and receive a deterministic, structured report. Preserve the existing metadata and "
    "artifact APIs, error behavior, and compatibility tests. Only files inside releaseguard/audit "
    "may be written. Use the available public tests to verify your implementation."
)

#: Phrases that would turn this pilot into a controlled experiment (stage B0). Their absence is a
#: blocking preflight check, not a convention.
FORBIDDEN_GOAL_PHRASES: tuple[str, ...] = (
    "you must delegate",
    "call delegate_tasks",
    "create two workers",
    "investigate metadata and artifacts in parallel",
    "this run evaluates your delegation behavior",
    "use subworkers before editing",
    "host-authored experiment instruction",
)

# -- live configuration ------------------------------------------------------

PROVIDER = "deepseek"
MODEL = "deepseek-chat"
IMAGE = "atlas-uat-python:3.12"
MAIN_MAX_TURNS = 24
TOKEN_BUDGET = 420_000
SANDBOX_TIMEOUT_MINUTES = 30
#: The whole shared audit layer, and nothing else. Metadata and artifacts are a compatibility
#: surface: a write there is denied by the ownership policy, not by a prompt.
MAIN_WRITE_PATHS: tuple[str, ...] = ("releaseguard/audit/",)
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

# -- the hidden oracle ------------------------------------------------------

#: A standalone assertion program. It is single-quoted on the shell command line, so it contains no
#: single quote, no backtick and no dollar sign anywhere. Every rule it asserts is visible to the Main
#: in ``releaseguard/audit/contracts.py``, the audit module docstrings or the public suite; nothing
#: here is invented at verification time.
HIDDEN_PROGRAM = """
import json
import shutil
import tempfile
from pathlib import Path

from releaseguard.artifacts import normalize_artifact_path, normalize_digest, scan_artifacts
from releaseguard.artifacts.errors import ArtifactPathError, DigestFormatError
from releaseguard.audit import (
    CHECK_CHECKSUMS,
    CHECK_COVERAGE,
    CHECK_EXTRAS,
    CHECK_INVENTORY,
    CHECK_LICENSE,
    CHECK_MANIFEST,
    CHECK_ORDER,
    CHECK_PATHS,
    CHECK_VERSION,
    AuditContractError,
    AuditIssue,
    AuditPolicy,
    CheckStatus,
    Severity,
    audit_release,
    build_report,
    render_report,
)
from releaseguard.audit.contracts import AuditCheck
from releaseguard.metadata import check_license, check_version, parse_manifest, parse_manifest_text

FAILURES = []


def check(condition, label):
    if not condition:
        FAILURES.append(label)


def statuses(report):
    return {item.check_id: item.status for item in report.checks}


def issues_of(report, check_id):
    return next(item for item in report.checks if item.check_id == check_id).issues


def payload():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


ORIGIN = Path.cwd()
SAMPLES = ORIGIN / "samples"
DIST = SAMPLES / "dist"
MANIFEST = SAMPLES / "release-manifest.json"
WORK = Path(tempfile.mkdtemp(prefix="oracle-"))


def write_manifest(name, document):
    path = WORK / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def fake_check(check_id, severity):
    if severity is None:
        return AuditCheck(check_id=check_id, status=CheckStatus.PASS)
    issue = AuditIssue(code="x", severity=severity, subject="s", message="m")
    status = CheckStatus.FAIL if severity is Severity.ERROR else CheckStatus.PASS
    return AuditCheck(check_id=check_id, status=status, issues=(issue,))


def simple_manifest_document():
    return {
        "release": {"name": "demo", "version": "1.0.0"},
        "license": {"spdx": "MIT"},
        "artifacts": [{"path": "demo.tar.gz", "sha256": "a" * 64, "size": 1}],
    }


# -- 1. the healthy release ---------------------------------------------------

report = audit_release(MANIFEST, DIST)
check(report.status is CheckStatus.PASS, "healthy status")
check(tuple(item.check_id for item in report.checks) == CHECK_ORDER, "healthy check order")
check(all(item.status is CheckStatus.PASS for item in report.checks), "healthy every check passes")
check(report.artifact_count == 4, "healthy artifact count")
check((report.manifest_name, report.manifest_version) == ("releaseguard", "2.4.0"), "healthy identity")
check(
    dict(report.summary)
    == {
        "checks": 8,
        "passed": 8,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "warnings": 1,
        "issues": 1,
    },
    "healthy summary",
)
extras = issues_of(report, CHECK_EXTRAS)
check([item.code for item in extras] == ["undeclared_artifact"], "extras code")
check(extras[0].subject == "extra-debug.log", "extras subject")
check(extras[0].severity is Severity.WARNING, "extras severity")
check(issues_of(report, CHECK_COVERAGE) == (), "healthy coverage empty")
check(issues_of(report, CHECK_CHECKSUMS) == (), "healthy checksums empty")
check(issues_of(report, CHECK_PATHS) == (), "healthy paths empty")

rendered = render_report(report)
check(rendered == render_report(audit_release(MANIFEST, DIST)), "render is stable")
check(rendered.endswith("\\n"), "render ends with a newline")
document = json.loads(rendered)
check(document["status"] == "pass", "render status")
check(
    list(report.as_dict())
    == ["status", "manifest", "artifact_count", "policy", "summary", "checks", "issues"],
    "as_dict key order",
)
check(
    list(report.as_dict()["summary"])
    == ["checks", "passed", "failed", "skipped", "errors", "warnings", "issues"],
    "summary key order",
)
check(document["manifest"] == {"name": "releaseguard", "version": "2.4.0"}, "render manifest")
check(str(SAMPLES) not in rendered, "no absolute path in the report")
check("workspace" not in rendered, "no workspace path in the report")
check(audit_release(str(MANIFEST), str(DIST)).as_dict() == report.as_dict(), "str paths accepted")

# -- 2. report assembly -------------------------------------------------------

built = build_report(
    [
        fake_check(CHECK_EXTRAS, None),
        fake_check(CHECK_CHECKSUMS, Severity.WARNING),
        fake_check(CHECK_MANIFEST, Severity.ERROR),
        fake_check(CHECK_VERSION, None),
    ]
)
check(
    [item.check_id for item in built.checks]
    == [CHECK_MANIFEST, CHECK_VERSION, CHECK_CHECKSUMS, CHECK_EXTRAS],
    "build_report orders by CHECK_ORDER",
)
check(
    [(item.severity.value, item.code) for item in built.issues]
    == [("error", "x"), ("warning", "x")],
    "build_report sorts issues by severity",
)
check(built.status is CheckStatus.FAIL, "build_report fails on an error")
check(built.summary["errors"] == 1 and built.summary["warnings"] == 1, "build_report counts")

duplicate_refused = False
try:
    build_report([fake_check(CHECK_MANIFEST, None), fake_check(CHECK_MANIFEST, None)])
except AuditContractError as error:
    duplicate_refused = getattr(error, "code", None) == "duplicate_check"
check(duplicate_refused, "build_report refuses a duplicate check")

skipped_only = build_report([AuditCheck(check_id=CHECK_MANIFEST, status=CheckStatus.SKIP)])
check(skipped_only.status is CheckStatus.PASS, "a skipped check is not a failure")
check(skipped_only.summary["skipped"] == 1, "skipped counted")

# -- 3. a manifest that cannot be read ---------------------------------------

broken = WORK / "broken.json"
broken.write_text("{not json", encoding="utf-8")
report = audit_release(broken, DIST)
state = statuses(report)
check(state[CHECK_MANIFEST] is CheckStatus.FAIL, "broken manifest fails")
check([item.code for item in issues_of(report, CHECK_MANIFEST)] == ["manifest_not_json"], "broken code")
check(state[CHECK_INVENTORY] is CheckStatus.PASS, "broken manifest still inventories")
check(report.artifact_count == 4, "broken manifest artifact count")
check(report.manifest_name is None, "broken manifest has no identity")
check(report.status is CheckStatus.FAIL, "broken manifest status")
for check_id in (CHECK_VERSION, CHECK_LICENSE, CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):
    check(state[check_id] is CheckStatus.SKIP, "broken skip " + check_id)

# -- 4. a declared artifact that is not there --------------------------------

document = payload()
document["artifacts"].append(
    {"path": "releaseguard-2.4.0-docs.zip", "sha256": "d" * 64, "size": 12}
)
report = audit_release(write_manifest("missing.json", document), DIST)
check(statuses(report)[CHECK_COVERAGE] is CheckStatus.FAIL, "coverage fails")
check(statuses(report)[CHECK_CHECKSUMS] is CheckStatus.PASS, "coverage does not touch checksums")
check(
    [item.code for item in issues_of(report, CHECK_COVERAGE)] == ["artifact_missing"],
    "coverage code",
)
check(
    issues_of(report, CHECK_COVERAGE)[0].subject == "releaseguard-2.4.0-docs.zip",
    "coverage subject",
)

# -- 5. digest and size disagreements ---------------------------------------

document = payload()
document["artifacts"][2]["sha256"] = "e" * 64
report = audit_release(write_manifest("digest.json", document), DIST)
check(
    [item.code for item in issues_of(report, CHECK_CHECKSUMS)] == ["digest_mismatch"],
    "digest mismatch code",
)
check(issues_of(report, CHECK_CHECKSUMS)[0].subject == "CHANGELOG.md", "digest mismatch subject")

document = payload()
document["artifacts"][2]["size"] = 1
report = audit_release(write_manifest("size.json", document), DIST)
check([item.code for item in issues_of(report, CHECK_CHECKSUMS)] == ["size_mismatch"], "size mismatch")

document = payload()
document["artifacts"][0]["sha256"] = "not-a-digest"
report = audit_release(write_manifest("malformed.json", document), DIST)
check([item.code for item in issues_of(report, CHECK_CHECKSUMS)] == ["digest_format"], "malformed digest")

# -- 6. one mistake, one finding --------------------------------------------

document = payload()
document["artifacts"].append({"path": "../escape.whl", "sha256": "f" * 64, "size": 3})
report = audit_release(write_manifest("escape.json", document), DIST)
check(statuses(report)[CHECK_PATHS] is CheckStatus.FAIL, "unsafe path fails the path check")
check(statuses(report)[CHECK_COVERAGE] is CheckStatus.PASS, "unsafe path is not also missing")
check([item.code for item in issues_of(report, CHECK_PATHS)] == ["path_escape"], "unsafe path code")
check(issues_of(report, CHECK_PATHS)[0].subject == "../escape.whl", "unsafe path subject")

# -- 7. policy ---------------------------------------------------------------

report = audit_release(MANIFEST, DIST, policy=AuditPolicy(allow_extra_artifacts=False))
check(statuses(report)[CHECK_EXTRAS] is CheckStatus.FAIL, "extras can be fatal")
check([item.severity for item in issues_of(report, CHECK_EXTRAS)] == [Severity.ERROR], "extras severity")
check(report.status is CheckStatus.FAIL, "extras status")

report = audit_release(MANIFEST, DIST, policy=AuditPolicy(fail_on_warnings=True))
check(statuses(report)[CHECK_EXTRAS] is CheckStatus.PASS, "promoted extras still pass the check")
check(report.status is CheckStatus.FAIL, "promoted status")
check(report.summary["errors"] == 0 and report.summary["warnings"] == 1, "promoted summary")

report = audit_release(MANIFEST, DIST, policy=AuditPolicy(allowed_licenses=("MIT",)))
check(
    [item.code for item in issues_of(report, CHECK_LICENSE)] == ["license_not_allowed"],
    "license policy",
)

# -- 8. metadata findings reach the report ----------------------------------

document = payload()
document["release"]["version"] = "2.4.0rc1"
report = audit_release(write_manifest("rc.json", document), DIST)
check(statuses(report)[CHECK_VERSION] is CheckStatus.FAIL, "version rule fails the check")
check([item.code for item in issues_of(report, CHECK_VERSION)] == ["prerelease_on_stable"], "version code")

document = payload()
document["license"]["spdx"] = "GPL-3.0"
report = audit_release(write_manifest("license.json", document), DIST)
check(
    [item.code for item in issues_of(report, CHECK_LICENSE)] == ["license_deprecated", "license_not_allowed"],
    "license codes",
)
check(
    [item.severity for item in issues_of(report, CHECK_LICENSE)] == [Severity.WARNING, Severity.ERROR],
    "license severities",
)

# -- 9. a build directory that is not there ---------------------------------

report = audit_release(MANIFEST, WORK / "absent")
check(statuses(report)[CHECK_INVENTORY] is CheckStatus.FAIL, "inventory fails")
check([item.code for item in issues_of(report, CHECK_INVENTORY)] == ["artifact_root_missing"], "inventory code")
check(report.artifact_count == 0, "inventory artifact count")
for check_id in (CHECK_PATHS, CHECK_CHECKSUMS, CHECK_COVERAGE, CHECK_EXTRAS):
    check(statuses(report)[check_id] is CheckStatus.SKIP, "inventory skip " + check_id)

# -- 10. the compatibility surface is unchanged -----------------------------

manifest = parse_manifest(MANIFEST)
check(manifest.channel == "stable", "compat channel")
check(manifest.python_requirement == ">=3.9", "compat python requirement")
check([item.kind for item in manifest.artifacts] == ["wheel", "sdist", "document"], "compat kinds")
check(manifest.artifact_paths()[2] == "CHANGELOG.md", "compat artifact order")

defaults = parse_manifest_text(json.dumps(simple_manifest_document()))
check(defaults.artifacts[0].kind == "binary", "compat default kind")
check(defaults.license.files == (), "compat default license files")
check(defaults.channel == "stable", "compat default channel")

duplicate_document = simple_manifest_document()
duplicate_document["artifacts"].append(dict(duplicate_document["artifacts"][0]))
duplicate_refused = False
try:
    parse_manifest_text(json.dumps(duplicate_document))
except Exception as error:
    duplicate_refused = getattr(error, "code", None) == "duplicate_artifact"
check(duplicate_refused, "compat duplicate artifact code")

inventory = scan_artifacts(DIST)
check(
    inventory.paths()
    == (
        "CHANGELOG.md",
        "extra-debug.log",
        "releaseguard-2.4.0-py3-none-any.whl",
        "releaseguard-2.4.0.tar.gz",
    ),
    "compat inventory order",
)
check(inventory.skipped == (), "compat inventory has no refusals")

path_refused = False
try:
    normalize_artifact_path("/etc/passwd")
except ArtifactPathError as error:
    path_refused = error.code == "path_absolute"
check(path_refused, "compat absolute path code")

digest_refused = False
try:
    normalize_digest("nope")
except DigestFormatError as error:
    digest_refused = error.code == "digest_format"
check(digest_refused, "compat digest code")

rc_document = simple_manifest_document()
rc_document["release"]["version"] = "1.0.0rc1"
check(
    [item.code for item in check_version(parse_manifest_text(json.dumps(rc_document)))]
    == ["prerelease_on_stable"],
    "compat version rule",
)
check(check_license(manifest) == (), "compat license rule")

# -- 11. the audit reads, never writes -------------------------------------

before = {item.name: item.read_bytes() for item in DIST.iterdir() if item.is_file()}
manifest_before = MANIFEST.read_bytes()
audit_release(MANIFEST, DIST)
after = {item.name: item.read_bytes() for item in DIST.iterdir() if item.is_file()}
check(before == after, "the audit wrote into the build directory")
check(MANIFEST.read_bytes() == manifest_before, "the audit wrote into the manifest")

shutil.rmtree(WORK, ignore_errors=True)
if FAILURES:
    raise SystemExit("HIDDEN_FAIL: " + "; ".join(FAILURES))
print("HIDDEN_OK")
"""

#: ``pytest`` first (the whole suite, including the two compatibility suites), then the hidden
#: program. Joined with ``&&`` so a failing suite can never be reported as an oracle pass.
ORACLE_COMMAND = f"python3 -m pytest -q && python3 -c '{HIDDEN_PROGRAM}'"


def acceptance() -> GoalAcceptance:
    spec = VerificationSpec(
        spec_id="stage-s1-releaseguard",
        version="1.0.0",
        deliverable_type="python-package",
        required_artifacts=(
            "releaseguard/audit/service.py",
            "releaseguard/audit/report.py",
        ),
        checks=(
            CheckSpec(
                check_id="service-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="releaseguard/audit/service.py",
            ),
            CheckSpec(
                check_id="report-present",
                category="PRESENCE",
                kind=CheckKind.FILE_PRESENT,
                artifact="releaseguard/audit/report.py",
            ),
            CheckSpec(
                check_id="service-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="releaseguard/audit/service.py",
            ),
            CheckSpec(
                check_id="report-parses",
                category="STRUCTURE",
                kind=CheckKind.PYTHON_PARSES,
                artifact="releaseguard/audit/report.py",
            ),
        ),
    ).with_declared_digest()
    return GoalAcceptance(
        spec=spec, dynamic_command=ORACLE_COMMAND, dynamic_timeout_seconds=420
    )


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def goal_digest() -> str:
    return digest(GOAL)


def prompt_v2_text() -> str:
    return PROMPT_V2_PATH.read_text(encoding="utf-8")


def prompt_v2_digest() -> str:
    """The identity of the prompt: a digest of the text the runtime injects."""

    return digest(prompt_v2_text())


def prompt_v2_file_digest() -> str:
    """Provenance of the file as checked out. Not the prompt's identity (see ``PROMPT_V2_DIGEST``)."""

    return digest_bytes(PROMPT_V2_PATH.read_bytes())


def forbidden_goal_phrase() -> str | None:
    """The first controlled-experiment phrase found in the goal, or ``None``."""

    lowered = GOAL.lower()
    for phrase in FORBIDDEN_GOAL_PHRASES:
        if phrase in lowered:
            return phrase
    return None


def frozen_identity() -> dict[str, object]:
    """Everything that must be identical before and after the single live attempt."""

    return {
        "stage": "S1",
        "experiment": "main_system_prompt_v2_real_model_pilot",
        "delegation_mode": "AUTONOMOUS_MAIN_DELEGATION",
        "goal": GOAL,
        "goal_digest": goal_digest(),
        "prompt": {
            "selected_version": PROMPT_V2_VERSION,
            "selected_digest": PROMPT_V2_DIGEST,
            "selected_digest_scope": "sha256 of the prompt text (AgentConfig.system_prompt)",
            "path": "experiments/stage_s1_prompt_v2/prompts/main_v2.md",
            "text_chars": len(prompt_v2_text()),
            "file_bytes": len(PROMPT_V2_PATH.read_bytes()),
            "file_digest": prompt_v2_file_digest(),
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
        "fixture_root": "experiments/fixtures/stage_s1_releaseguard",
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
    "PROMPT_V2_DIGEST",
    "PROMPT_V2_PATH",
    "PROMPT_V2_VERSION",
    "PROVIDER",
    "PUBLIC_TESTS",
    "RESULT_PREVIEW_CHARS",
    "SANDBOX_TIMEOUT_MINUTES",
    "TOKEN_BUDGET",
    "TOOL_ERROR_MODE",
    "acceptance",
    "digest",
    "digest_bytes",
    "forbidden_goal_phrase",
    "frozen_identity",
    "goal_digest",
    "prompt_v2_digest",
    "prompt_v2_file_digest",
    "prompt_v2_text",
]
