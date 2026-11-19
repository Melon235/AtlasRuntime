"""Stage P1b offline readiness: is the fixture solvable, and is the oracle discriminating?

Runs no sandbox and no model. It copies the fixture into a throwaway directory, then answers four
questions:

1. **Baseline** — with the shipped code, do the two plugin suites pass and the unified contract
   suite fail?
2. **Solvability** — with the reference implementation applied (``reference/core/*.py``), do the
   public suite and the hidden oracle both pass?
3. **Discrimination** — does the hidden oracle reject the unimplemented fixture and five plausible
   wrong implementations, and does the public suite alone miss some of them?
4. **Isolation** — is the hidden programme absent from every file the run provisions?

    python experiments/stage_p1b_live/verify_offline.py --freeze
    python experiments/stage_p1b_live/verify_offline.py

``--freeze`` (re)writes ``fixture_manifest.json``; the live runner refuses to start when the
fixture's current digests differ from that file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(HERE))

from p1b_contract import (  # noqa: E402
    FIXTURE_MANIFEST,
    FIXTURE_ROOT,
    FIXTURE_SKIP,
    GOAL,
    HIDDEN_PROGRAM,
    MAIN_MAX_TURNS,
    MAIN_WRITE_PATHS,
    ORACLE_COMMAND,
    PUBLIC_TESTS,
    TOKEN_BUDGET,
)

REFERENCE = HERE / "reference" / "core"
#: Reference modules, and the fixture modules they replace.
REFERENCE_TARGETS = {"adapter.py": "core/adapter.py", "registry.py": "core/registry.py"}

#: Each wrong implementation: a textual substitution applied to the reference source.
WRONG_IMPLEMENTATIONS: dict[str, list[tuple[str, str, str]]] = {
    "unimplemented": [],
    "trace_level_as_info": [
        (
            "adapter.py",
            '        severity=Severity(severity_of(record.get("level"))),',
            "        severity=Severity(\n"
            '            "info"\n'
            '            if (record.get("level") or "").strip().lower() == "trace"\n'
            '            else severity_of(record.get("level"))\n'
            "        ),",
        )
    ],
    "priority_one_as_info": [
        (
            "adapter.py",
            '        severity=Severity(severity_of(record.get("priority"))),',
            "        severity=Severity(\n"
            '            "info"\n'
            '            if record.get("priority") == 1\n'
            '            else severity_of(record.get("priority"))\n'
            "        ),",
        )
    ],
    "file_tags_not_split": [
        (
            "adapter.py",
            '        tags=split_tags(record.get("tags")),',
            '        tags=(str(record.get("tags")),) if record.get("tags") else (),',
        )
    ],
    "message_id_without_prefix": [
        (
            "adapter.py",
            '        event_id=f"msg-{native_id}",',
            "        event_id=str(native_id),",
        )
    ],
    "dispatch_unsorted": [
        (
            "registry.py",
            "        return tuple(sorted(events, key=lambda event: "
            "(event.occurred_at, event.event_id)))",
            "        return tuple(events)",
        )
    ],
}


def fixture_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in FIXTURE_SKIP for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return files


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    return {name: digest(root / name) for name in fixture_files(root)}


def materialise(destination: Path) -> Path:
    for name in fixture_files(FIXTURE_ROOT):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE_ROOT / name, target)
    return destination


def apply_reference(destination: Path, variant: str = "reference") -> None:
    if variant == "unimplemented":
        return
    for source, relative in REFERENCE_TARGETS.items():
        text = (REFERENCE / source).read_text(encoding="utf-8")
        for module, old, new in WRONG_IMPLEMENTATIONS.get(variant, []):
            if module != source:
                continue
            if old not in text:
                raise SystemExit(f"variant {variant!r}: anchor not found in {source}")
            text = text.replace(old, new)
        (destination / relative).write_text(text, encoding="utf-8")


def run(command: list[str], *, cwd: Path) -> dict[str, Any]:
    completed = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, timeout=600)
    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "command": command,
        "exit_code": completed.returncode,
        "passed": completed.returncode == 0,
        "output_chars": len(output),
        "output_tail": output.strip().splitlines()[-6:],
        "hidden_ok": "HIDDEN_OK" in output,
    }


def run_public(target: Path) -> dict[str, Any]:
    command = [sys.executable, "-B", "-m", "pytest", "-q", "tests"]
    result = run(command, cwd=target)
    result["contract_command"] = PUBLIC_TESTS.command
    return result


def run_hidden(target: Path) -> dict[str, Any]:
    result = run([sys.executable, "-c", HIDDEN_PROGRAM], cwd=target)
    result["contract_command"] = ORACLE_COMMAND.split("&&")[1].strip()
    return result


def check_isolation() -> dict[str, Any]:
    """The hidden programme must not be reachable from anything the run provisions."""

    marker = "HIDDEN_OK"
    leaks: list[str] = []
    for name in fixture_files(FIXTURE_ROOT):
        text = (FIXTURE_ROOT / name).read_text(encoding="utf-8", errors="replace")
        if marker in text or "HIDDEN_FAIL" in text:
            leaks.append(name)
    return {
        "fixture_files_scanned": len(fixture_files(FIXTURE_ROOT)),
        "marker": marker,
        "leaks": leaks,
        "public_and_oracle_commands_differ": PUBLIC_TESTS.command != ORACLE_COMMAND,
        "oracle_reachable_as_a_tool": False,
        "note": (
            "the oracle is one host-held command string; it is not registered as a tool and is "
            "never written into the workspace"
        ),
    }


def write_freeze() -> dict[str, Any]:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "fixture_root": str(FIXTURE_ROOT.relative_to(REPO_ROOT)),
        "files": manifest(FIXTURE_ROOT),
        "goal_digest": "sha256:" + hashlib.sha256(GOAL.encode("utf-8")).hexdigest(),
        "oracle_command_digest": "sha256:"
        + hashlib.sha256(ORACLE_COMMAND.encode("utf-8")).hexdigest(),
        "public_command_digest": "sha256:"
        + hashlib.sha256(PUBLIC_TESTS.command.encode("utf-8")).hexdigest(),
        "main_write_paths": list(MAIN_WRITE_PATHS),
        "main_max_turns": MAIN_MAX_TURNS,
        "token_budget": TOKEN_BUDGET,
    }
    FIXTURE_MANIFEST.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def verify_freeze() -> dict[str, Any]:
    if not FIXTURE_MANIFEST.exists():
        return {"frozen": False, "matches": False, "reason": "fixture_manifest.json is missing"}
    frozen = json.loads(FIXTURE_MANIFEST.read_text(encoding="utf-8"))
    expected = frozen.get("files") or {}
    actual = manifest(FIXTURE_ROOT)
    changed = sorted(
        name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name)
    )
    return {
        "frozen": True,
        "matches": not changed,
        "changed": changed,
        "file_count": len(expected),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage P1b offline readiness")
    parser.add_argument("--freeze", action="store_true", help="rewrite fixture_manifest.json")
    parser.add_argument("--keep", action="store_true", help="keep the scratch tree")
    args = parser.parse_args(argv)

    if args.freeze:
        payload = write_freeze()
        print(json.dumps({"frozen": len(payload["files"])}, indent=2))

    scratch = Path(tempfile.mkdtemp(prefix="atlas-p1b-offline-"))
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scratch": str(scratch),
        "freeze": verify_freeze(),
        "isolation": check_isolation(),
        "fixture_files": len(fixture_files(FIXTURE_ROOT)),
        "baseline": {},
        "reference": {},
        "variants": {},
    }
    try:
        baseline = materialise(scratch / "baseline")
        plugin_suites = run(
            [
                sys.executable,
                "-B",
                "-m",
                "pytest",
                "-q",
                "tests/test_file_plugin.py",
                "tests/test_message_plugin.py",
            ],
            cwd=baseline,
        )
        contract_suite = run(
            [sys.executable, "-B", "-m", "pytest", "-q", "tests/test_public_contract.py"],
            cwd=baseline,
        )
        public = run_public(baseline)
        hidden = run_hidden(baseline)
        report["baseline"] = {
            "plugin_suites": plugin_suites,
            "contract_suite": contract_suite,
            "public": public,
            "hidden": hidden,
            "plugin_suites_pass": plugin_suites["passed"],
            "contract_suite_fails": not contract_suite["passed"],
            "oracle_fails": not (public["passed"] and hidden["passed"]),
        }

        for variant in WRONG_IMPLEMENTATIONS:
            target = materialise(scratch / variant)
            apply_reference(target, variant)
            public = run_public(target)
            hidden = run_hidden(target)
            report["variants"][variant] = {
                "public_passed": public["passed"],
                "oracle_passed": public["passed"] and hidden["passed"],
                "hidden_exit": hidden["exit_code"],
                "hidden_ok": hidden["hidden_ok"],
                "hidden_tail": hidden["output_tail"],
            }

        reference = materialise(scratch / "solved")
        apply_reference(reference)
        public = run_public(reference)
        hidden = run_hidden(reference)
        report["reference"] = {
            "public": public,
            "hidden": hidden,
            "solved": public["passed"] and hidden["passed"] and hidden["hidden_ok"],
        }
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)

    expectations = {
        "freeze_matches": bool(report["freeze"].get("matches")),
        "plugin_suites_pass": bool(report["baseline"]["plugin_suites_pass"]),
        "contract_suite_fails": bool(report["baseline"]["contract_suite_fails"]),
        "baseline_public_fails": not report["baseline"]["public"]["passed"],
        "baseline_oracle_fails": report["baseline"]["oracle_fails"],
        "reference_solved": report["reference"]["solved"],
        "unimplemented_rejected": not report["variants"]["unimplemented"]["oracle_passed"],
        "all_wrong_rejected": all(
            not item["oracle_passed"] for item in report["variants"].values()
        ),
        "oracle_beyond_public": any(
            item["public_passed"] and not item["oracle_passed"]
            for item in report["variants"].values()
        ),
        "oracle_isolated": not report["isolation"]["leaks"],
        "commands_differ": report["isolation"]["public_and_oracle_commands_differ"],
    }
    report["expectations"] = expectations
    report["ready"] = all(expectations.values())
    (HERE / "offline_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(expectations, indent=2, sort_keys=True))
    print(
        json.dumps(
            {
                name: {
                    "public": item["public_passed"],
                    "oracle": item["oracle_passed"],
                    "hidden_tail": item["hidden_tail"][-1:],
                }
                for name, item in report["variants"].items()
            },
            indent=2,
            sort_keys=True,
        )
    )
    print(f"READY={report['ready']}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
