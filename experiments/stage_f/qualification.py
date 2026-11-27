"""F2-B task qualification: seed/reference runs, mutations, complexity, DAG and leak checks.

Everything here is offline and deterministic: pytest runs in a temporary copy of a materialised
fixture, no provider, no sandbox, no claim. The output is a JSON evidence document that the stage
report cites.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.stage_f import tasks
from experiments.stage_f.mutations import MUTATIONS, MUTATIONS_BY_TASK, Mutation

REPO = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent / "task-qualification.json"
STATUS_PASS = "STAGE_F_TASK_QUALIFICATION_PASS"
STATUS_INCOMPLETE = "STAGE_F_TASK_QUALIFICATION_INCOMPLETE"

PASS = "PASS"
BUSINESS_FAILURE = "BUSINESS_FAILURE"
COLLECTION_ERROR = "COLLECTION_ERROR"
NO_TESTS = "NO_TESTS_COLLECTED"
TIMEOUT = "TIMEOUT"
ENVIRONMENT = "ENVIRONMENT_FAILURE"
NONZERO_OTHER = "NONZERO_OTHER"

FAILING_CATEGORIES = {BUSINESS_FAILURE, COLLECTION_ERROR, NONZERO_OTHER}
UNRUNNABLE_CATEGORIES = {COLLECTION_ERROR, NO_TESTS, TIMEOUT, ENVIRONMENT}

#: The two reused Closure-2 seeds are byte-for-byte historical fixtures. S2's public suite imports
#: the v2 `reject`/`cancel` callers, which the v1 seed does not define, so its seed failure is an
#: *expected* collection failure rather than a business assertion failure.
EXPECTED_SEED_COLLECTION: dict[str, str] = {
    "closure2-state-machine-refactor": (
        "the public suite imports the v2 reject/cancel callers, which the v1 seed does not define"
    ),
}


@dataclass(frozen=True)
class RunResult:
    rc: int
    category: str
    summary: str
    tail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rc": self.rc,
            "category": self.category,
            "summary": self.summary,
            "tail": self.tail,
        }


def _write(root: Path, files: Mapping[str, str]) -> None:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _classify(rc: int, output: str) -> str:
    if rc == 0:
        return PASS
    if "no tests ran" in output or "collected 0 items" in output:
        return NO_TESTS
    if (
        "ERROR collecting" in output
        or "errors during collection" in output
        or "ImportError while importing test module" in output
    ):
        return COLLECTION_ERROR
    if "INTERNALERROR" in output:
        return ENVIRONMENT
    if "failed" in output or "FAILED " in output or "DID NOT RAISE" in output:
        return BUSINESS_FAILURE
    return NONZERO_OTHER


def _run(workspace: Path, target: str) -> RunResult:
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", target],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired as error:
        output = f"{error.stdout or ''}{error.stderr or ''}"
        return RunResult(rc=-1, category=TIMEOUT, summary="pytest timed out", tail=output[-400:])
    output = completed.stdout + completed.stderr
    lines = [line for line in output.strip().splitlines() if line.strip()]
    summary = lines[-1] if lines else ""
    return RunResult(
        rc=completed.returncode,
        category=_classify(completed.returncode, output),
        summary=summary[:300],
        tail=output[-400:],
    )


def _merged(task: Any) -> dict[str, str]:
    files = dict(task.seed)
    files.update(task.reference)
    return files


def _apply(files: Mapping[str, str], mutation: Mutation) -> tuple[dict[str, str], list[str]]:
    updated = dict(files)
    problems: list[str] = []
    for path, old, new in mutation.edits:
        if path not in updated:
            problems.append(f"{mutation.mutation_id}: {path} is not in the reference workspace")
            continue
        count = updated[path].count(old)
        if count != 1:
            problems.append(f"{mutation.mutation_id}: edit anchor occurs {count} times in {path}")
            continue
        updated[path] = updated[path].replace(old, new)
    return updated, problems


def _hidden_name(task: Any) -> str | None:
    return next((name for name in task.oracle_tests if name.startswith("test_hidden_")), None)


def qualify_task(task: Any) -> dict[str, Any]:
    scratch = Path(tempfile.mkdtemp(prefix=f"f2b-q-{task.task_id[:10]}-"))
    hidden = _hidden_name(task)
    if hidden is None:
        return {"task_id": task.task_id, "qualified": False, "problems": ["no hidden oracle file"]}

    seed_root = scratch / "seed"
    _write(seed_root, dict(task.seed))
    _write(seed_root, dict(task.public_tests))
    reference_root = scratch / "reference"
    _write(reference_root, _merged(task))
    _write(reference_root, dict(task.public_tests))
    oracle_seed_root = scratch / "oracle-seed"
    _write(oracle_seed_root, dict(task.seed))
    _write(oracle_seed_root, dict(task.oracle_tests))
    oracle_reference_root = scratch / "oracle-reference"
    _write(oracle_reference_root, _merged(task))
    _write(oracle_reference_root, dict(task.oracle_tests))

    seed_public = _run(seed_root, "test_public.py")
    seed_oracle = _run(oracle_seed_root, hidden)
    ref_public = _run(reference_root, "test_public.py")
    ref_oracle = _run(oracle_reference_root, hidden)

    expected_collection = task.task_id in EXPECTED_SEED_COLLECTION
    problems: list[str] = []
    for label, result in (("seed public", seed_public), ("seed oracle", seed_oracle)):
        if result.category == COLLECTION_ERROR and expected_collection:
            continue
        if result.category in UNRUNNABLE_CATEGORIES:
            problems.append(f"{label} did not run a business assertion ({result.category})")
        elif result.category == PASS:
            problems.append(f"{label} unexpectedly passed")
    if ref_public.category != PASS:
        problems.append(f"reference public suite: {ref_public.category}")
    if ref_oracle.category != PASS:
        problems.append(f"reference oracle: {ref_oracle.category}")

    return {
        "task_id": task.task_id,
        "family": tasks.family_of(task.task_id),
        "seed_public": seed_public.as_dict(),
        "seed_oracle": seed_oracle.as_dict(),
        "reference_public": ref_public.as_dict(),
        "reference_oracle": ref_oracle.as_dict(),
        "seed_failure": (
            "EXPECTED_SEED_COLLECTION_FAILURE"
            if expected_collection and seed_public.category == COLLECTION_ERROR
            else seed_public.category
        ),
        "expected_seed_collection_reason": EXPECTED_SEED_COLLECTION.get(task.task_id),
        "qualified": not problems,
        "problems": problems,
    }


def qualify_mutations(
    task: Any, extra: tuple[Mutation, ...] = ()
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    hidden = _hidden_name(task)
    if hidden is None:
        return rows
    for mutation in (*MUTATIONS_BY_TASK.get(task.task_id, ()), *extra):
        files, problems = _apply(_merged(task), mutation)
        scratch = Path(tempfile.mkdtemp(prefix=f"f2b-m-{mutation.mutation_id[:14]}-"))
        public_root = scratch / "public"
        _write(public_root, files)
        _write(public_root, dict(task.public_tests))
        oracle_root = scratch / "oracle"
        _write(oracle_root, files)
        _write(oracle_root, dict(task.oracle_tests))
        public = _run(public_root, "test_public.py")
        oracle = _run(oracle_root, hidden)
        loads = oracle.category not in UNRUNNABLE_CATEGORIES
        detected = oracle.category == BUSINESS_FAILURE
        valid = loads and detected and not problems
        if not loads:
            problems.append(f"oracle did not run a business assertion ({oracle.category})")
        if loads and not detected:
            problems.append("the independent oracle did not detect the mutation")
        rows.append(
            {
                "mutation_id": mutation.mutation_id,
                "task_id": mutation.task_id,
                "rule": mutation.rule,
                "semantics": mutation.semantics,
                "files": list(mutation.files),
                "public": public.as_dict(),
                "oracle": oracle.as_dict(),
                "program_loads": loads,
                "detected_by_public": public.category == BUSINESS_FAILURE,
                "detected_by_oracle": detected,
                "valid": valid,
                "problems": problems,
            }
        )
    return rows


# -- static checks -----------------------------------------------------------------------------


def _allowed(path: str, allowed: Iterable[str]) -> bool:
    for entry in allowed:
        if path == entry:
            return True
        if entry.endswith("/") and path.startswith(entry):
            return True
        if fnmatch.fnmatch(path, entry):
            return True
    return False


def _forbidden(path: str, forbidden: Iterable[str]) -> bool:
    for entry in forbidden:
        if path == entry:
            return True
        if entry.endswith("/") and path.startswith(entry):
            return True
        if fnmatch.fnmatch(path, entry):
            return True
    return False


def _parallel_width(workers: list[dict[str, Any]]) -> int:
    depth: dict[str, int] = {}
    by_id = {str(worker["task_id"]): worker for worker in workers}

    def level(node: str) -> int:
        if node in depth:
            return depth[node]
        worker = by_id[node]
        value = 1 + max((level(dep) for dep in worker["depends_on"]), default=-1)
        depth[node] = value
        return value

    for node in by_id:
        level(node)
    moments: dict[int, int] = {}
    for value in depth.values():
        moments[value] = moments.get(value, 0) + 1
    return max(moments.values(), default=0)


def static_dag_checks(task: Any, files: Mapping[str, str]) -> dict[str, Any]:
    problems: list[str] = []
    workers = list(task.workers)
    by_id = {str(worker["task_id"]): worker for worker in workers}
    if len(by_id) != len(workers):
        problems.append("duplicate worker ids")

    for worker in workers:
        node = str(worker["task_id"])
        for dependency in worker["depends_on"]:
            if dependency not in by_id:
                problems.append(f"{node} depends on unknown node {dependency}")
        if node in worker["depends_on"]:
            problems.append(f"{node} depends on itself")
        for path in worker["owned_paths"]:
            if not _allowed(path, task.allowed_paths):
                problems.append(f"{node} owns a path outside allowed_paths: {path}")
            if _forbidden(path, task.forbidden_paths):
                problems.append(f"{node} owns a forbidden path: {path}")
            if path not in files:
                problems.append(f"{node} owns a missing artifact: {path}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visited:
            return
        if node in visiting:
            problems.append(f"dependency cycle through {node}")
            return
        visiting.add(node)
        for dependency in by_id[node]["depends_on"]:
            if dependency in by_id:
                visit(dependency)
        visiting.discard(node)
        visited.add(node)

    for node in by_id:
        visit(node)

    for index, left in enumerate(workers):
        for right in workers[index + 1 :]:
            left_id = str(left["task_id"])
            right_id = str(right["task_id"])
            if right_id in left["depends_on"] or left_id in right["depends_on"]:
                continue
            overlap = set(left["owned_paths"]) & set(right["owned_paths"])
            if overlap:
                problems.append(f"{left_id} and {right_id} share {sorted(overlap)}")

    covered = {path for worker in workers for path in worker["owned_paths"]}
    missing = [path for path in task.deliverables if path not in covered]
    if missing:
        problems.append(f"deliverables not owned by any node: {missing}")
    for path in task.deliverables:
        if path not in files:
            problems.append(f"deliverable does not exist in the final workspace: {path}")

    return {
        "nodes": [
            {
                "task_id": str(worker["task_id"]),
                "depends_on": list(worker["depends_on"]),
                "owned_paths": list(worker["owned_paths"]),
                "max_turns": int(worker.get("max_turns", 12)),
            }
            for worker in workers
        ],
        "edges": sum(len(worker["depends_on"]) for worker in workers),
        "parallel_width": _parallel_width(workers),
        "problems": problems,
        "valid": not problems,
    }


FORBIDDEN_PROMPT_TOKENS = (
    "a0",
    "a1",
    "a2",
    "a3",
    "static dag",
    "router",
    "mutation",
    "reference solution",
    "hidden oracle",
    "execution arm",
    "cell id",
    "group assignment",
)


def leak_checks(task: Any) -> dict[str, Any]:
    problems: list[str] = []
    for name, text in task.seed.items():
        lowered = name.lower()
        if "test_hidden" in lowered or "oracle" in lowered or "reference" in lowered:
            problems.append(f"seed carries an evaluation artifact: {name}")
        if "test_hidden" in text or "reference implementation" in text.lower():
            problems.append(f"seed text mentions hidden material: {name}")
    for name in task.oracle_tests:
        if name in task.seed or name in task.public_tests:
            problems.append(f"oracle file is staged publicly: {name}")
    for name, text in task.public_tests.items():
        if "test_hidden" in text:
            problems.append(f"public suite references the oracle: {name}")
    final = _merged(task)
    for name, text in task.reference.items():
        if name in task.seed and task.seed[name] == text:
            problems.append(f"reference lists an unchanged file: {name}")
        for seed_name, seed_text in task.seed.items():
            if len(text) > 200 and text in seed_text:
                problems.append(f"reference content is embedded in {seed_name}: {name}")
    prompt = f"{task.prompt}\n{task.shared_context}".lower()
    for token in FORBIDDEN_PROMPT_TOKENS:
        pattern = rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])"
        if re.search(pattern, prompt):
            problems.append(f"public prompt leaks the evaluation design: {token!r}")
    diagnostics = {
        name: text for name, text in task.seed.items() if name.startswith("diagnostics/")
    }
    for name, text in diagnostics.items():
        lowered = text.lower()
        if "the fix is" in lowered or "solution:" in lowered:
            problems.append(f"diagnostic evidence contains a solution: {name}")
    return {"files_checked": len(final), "problems": problems, "valid": not problems}


# -- complexity --------------------------------------------------------------------------------


def _line_count(files: Mapping[str, str], predicate) -> int:
    return sum(len(text.splitlines()) for name, text in files.items() if predicate(name))


def _is_business(name: str) -> bool:
    return name.endswith(".py") and name != "test_public.py" and not name.startswith("diagnostics/")


def _is_docs(name: str) -> bool:
    return name.endswith(".md")


def _is_diagnostic(name: str) -> bool:
    return name.startswith("diagnostics/") and not name.endswith(".py")


def _imports(text: str) -> set[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return names


def complexity(task: Any) -> dict[str, Any]:
    final = _merged(task)
    business = {name: text for name, text in final.items() if _is_business(name)}
    seed_business = {name: text for name, text in task.seed.items() if _is_business(name)}
    local_roots = {name.split("/")[0] for name in business}
    imported: set[str] = set()
    for text in business.values():
        imported.update(_imports(text))
    external = sorted(
        name
        for name in imported
        if name not in sys.stdlib_module_names and name not in local_roots and name != "__future__"
    )
    added = deleted = 0
    changed_lines: dict[str, dict[str, int]] = {}
    for name, text in task.reference.items():
        before = task.seed.get(name, "").splitlines(keepends=True)
        after = text.splitlines(keepends=True)
        diff = list(difflib.unified_diff(before, after, n=0))
        file_added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        file_deleted = sum(
            1 for line in diff if line.startswith("-") and not line.startswith("---")
        )
        added += file_added
        deleted += file_deleted
        changed_lines[name] = {"added": file_added, "deleted": file_deleted}
    return {
        "business_source_loc": _line_count(business, lambda name: True),
        "seed_business_source_loc": _line_count(seed_business, lambda name: True),
        "documentation_loc": _line_count(task.seed, _is_docs),
        "public_test_loc": _line_count(task.public_tests, lambda name: True),
        "oracle_loc": _line_count(task.oracle_tests, lambda name: True),
        "diagnostic_data_lines": _line_count(task.seed, _is_diagnostic),
        "diagnostic_data_bytes": sum(
            len(text.encode("utf-8")) for name, text in task.seed.items() if _is_diagnostic(name)
        ),
        "reference_changed_files": len(task.reference),
        "reference_added_lines": added,
        "reference_deleted_lines": deleted,
        "reference_changed_lines": added + deleted,
        "per_file_diff": changed_lines,
        "modules": len(business),
        "local_packages": len(local_roots),
        "seeded_files": len(task.seed),
        "external_dependencies": external,
    }


# -- report ------------------------------------------------------------------------------------


def build_report(*, only: str | None = None) -> dict[str, Any]:
    selected = [task for task in tasks.TASKS if only is None or task.task_id == only]
    qualification: list[dict[str, Any]] = []
    mutations: list[dict[str, Any]] = []
    complexity_rows: list[dict[str, Any]] = []
    dag_rows: list[dict[str, Any]] = []
    leak_rows: list[dict[str, Any]] = []
    for task in selected:
        final = _merged(task)
        qualification.append(qualify_task(task))
        mutations.extend(qualify_mutations(task))
        complexity_rows.append({"task_id": task.task_id, **complexity(task)})
        dag_rows.append({"task_id": task.task_id, **static_dag_checks(task, final)})
        leak_rows.append({"task_id": task.task_id, **leak_checks(task)})

    qualified = [row["qualified"] for row in qualification]
    mutation_valid = [row["valid"] for row in mutations]
    dag_valid = [row["valid"] for row in dag_rows]
    leak_valid = [row["valid"] for row in leak_rows]
    expected_tasks = 8 if only is None else 1
    expected_mutations = len(MUTATIONS) if only is None else len(MUTATIONS_BY_TASK.get(only, ()))
    checks = {
        "eight_tasks": len(qualification) == expected_tasks,
        "all_tasks_qualified": bool(qualified) and all(qualified),
        "mutation_count": len(mutations),
        "mutation_minimum_met": len(mutations) >= max(expected_mutations, 1),
        "all_mutations_valid": bool(mutation_valid) and all(mutation_valid),
        "all_static_dags_valid": bool(dag_valid) and all(dag_valid),
        "all_leak_checks_valid": bool(leak_valid) and all(leak_valid),
    }
    other_checks = [value for key, value in checks.items() if key != "mutation_count"]
    status = STATUS_PASS if all(other_checks) else STATUS_INCOMPLETE
    return {
        "stage": "F2-B",
        "status": status,
        "tasks": list(tasks.TASK_IDS) if only is None else [only],
        "checks": checks,
        "qualification": qualification,
        "mutations": mutations,
        "complexity": complexity_rows,
        "static_dag": dag_rows,
        "leak_checks": leak_rows,
        "notes": [
            "no model request and no sandbox container was used",
            "the reference is used only to prove oracle satisfiability and to build mutations",
            "the historical master manifest and batch plans are not refrozen in this stage",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=None, help="qualify one task id only")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    report = build_report(only=args.task)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"status: {report['status']}")
    print(f"checks: {json.dumps(report['checks'], sort_keys=True)}")
    for row in report["qualification"]:
        print(
            f"{row['task_id']}: qualified={row['qualified']} "
            f"seed_public={row['seed_public']['category']} "
            f"seed_oracle={row['seed_oracle']['category']} "
            f"ref_public={row['reference_public']['category']} "
            f"ref_oracle={row['reference_oracle']['category']}"
        )
        for problem in row["problems"]:
            print(f"  problem: {problem}")
    for row in report["mutations"]:
        print(
            f"{row['mutation_id']}: valid={row['valid']} "
            f"public={row['public']['category']} oracle={row['oracle']['category']}"
        )
        for problem in row["problems"]:
            print(f"  problem: {problem}")
    return 0 if report["status"] == STATUS_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
