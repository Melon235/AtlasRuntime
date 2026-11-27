"""Qualify the revised task contracts offline, without freezing a release."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.catalog import build_task, task_digests
from experiments.stage_b_plus.authority import digest_of
from experiments.stage_b_plus.executor import prepare_fixture
from experiments.stage_f import qualification as q
from experiments.stage_f.mutations import Mutation
from experiments.stage_f.tasks import Task
from experiments.stage_f.tasks.revisions import C1, REVISIONS, X2, task_at_revision

_TLS = """    if checked["tls.enabled"]:
        for path in ("tls.cert", "tls.key"):
            if not checked[path]:
                raise ConstraintViolation(path, f"tls.enabled requires {path}")"""
_PERSIST = """        if self.journal is not None:
            self.journal.record_take(retried)"""
_LOADER = "configkit/loader.py"
_PROFILE = "configkit/profile.py"
_VALIDATE = "configkit/validate.py"
_BROKER = "eventsys/broker.py"


def _mutation(task_id: str, name: str, rule: str, edit: tuple[str, str, str]) -> Mutation:
    return Mutation(name, task_id, rule, "Breaks " + rule, (edit,))


_NEW_MUTATIONS = {
    C1: (
        _mutation(
            C1,
            "C1-R2-preset",
            "the complete preset table",
            (_PROFILE, '"cache.ttl": 300.0', '"cache.ttl": 301.0'),
        ),
        _mutation(
            C1,
            "C1-R2-copy",
            "independent preset lists",
            (_PROFILE, "return deepcopy(PROFILES[name])", "return dict(PROFILES[name])"),
        ),
        _mutation(
            C1,
            "C1-R2-tls-order",
            "TLS cert-first ordering",
            (
                _VALIDATE,
                'for path in ("tls.cert", "tls.key")',
                'for path in ("tls.key", "tls.cert")',
            ),
        ),
        _mutation(
            C1,
            "C1-R2-tls-key-path",
            "the missing TLS field error path",
            (_VALIDATE, "raise ConstraintViolation(path,", 'raise ConstraintViolation("tls.cert",'),
        ),
        _mutation(
            C1,
            "C1-R2-env-case",
            "the exact environment prefix",
            (_LOADER, "key.startswith(PREFIX)", "key.upper().startswith(PREFIX)"),
        ),
        _mutation(
            C1,
            "C1-R2-env-empty-path",
            "the empty environment path rejection",
            (
                _LOADER,
                'raise ParseError(key, "environment key has an empty path")',
                'mapped[""] = value\n            continue',
            ),
        ),
        _mutation(
            C1,
            "C1-R2-env-empty-segment",
            "the empty environment segment rejection",
            (
                _LOADER,
                "if any(not part for part in parts):",
                "if any(part == '.' for part in parts):",
            ),
        ),
        _mutation(
            C1,
            "C1-R2-env-type",
            "non-string environment value rejection",
            (
                _LOADER,
                'raise ParseError(key, "environment values must be strings")',
                "value = str(value)",
            ),
        ),
        _mutation(
            C1,
            "C1-R2-env-path",
            "the original environment key as error path",
            (
                _LOADER,
                'raise ParseError(key, "environment key has an empty path")',
                'raise ParseError("", "environment key has an empty path")',
            ),
        ),
    ),
    X2: (
        _mutation(
            X2,
            "X2-R2-publish-type",
            "ValueError for a non-Event publish argument",
            (
                _BROKER,
                'raise ValueError("publish needs an Event")',
                'raise TypeError("publish needs an Event")',
            ),
        ),
        _mutation(
            X2,
            "X2-R2-nack-persistence",
            "durable nack attempts before redelivery",
            (_BROKER, _PERSIST, ""),
        ),
    ),
}

_EQUIVALENT = {
    C1: Mutation(
        "C1-R2-equivalent",
        C1,
        "preset copy and TLS paths",
        "Copy only mutable preset values",
        (
            (
                _PROFILE,
                "return deepcopy(PROFILES[name])",
                "return {key: list(value) if isinstance(value, list) else value\n"
                "            for key, value in PROFILES[name].items()}",
            ),
            (
                _VALIDATE,
                _TLS,
                """    if checked["tls.enabled"] and not checked["tls.cert"]:
        raise ConstraintViolation("tls.cert", "cert missing")
    if checked["tls.enabled"] and not checked["tls.key"]:
        raise ConstraintViolation("tls.key", "key missing")""",
            ),
        ),
    ),
    X2: Mutation(
        "X2-R2-equivalent",
        X2,
        "durable nack attempts",
        "Persist before enqueuing without changing recovery",
        (
            (
                _BROKER,
                "        self._items.appendleft(retried)\n" + _PERSIST,
                _PERSIST + "\n        self._items.appendleft(retried)",
            ),
        ),
    ),
}


def _identity(task: Task, revision: int) -> dict[str, Any]:
    digests = task_digests(task)
    digests["public_specification_digest"] = digest_of(task.seed["docs/INTERFACE.md"])
    return {
        "revision": revision,
        **digests,
        "task_digest": digest_of({"task_id": task.task_id, "revision": revision, **digests}),
    }


def _equivalent(task: Task, evidence: Path) -> dict[str, Any]:
    files, problems = q._apply(q._merged(task), _EQUIVALENT[task.task_id])
    suites = {}
    for name, tests, entry in (
        ("public", task.public_tests, "test_public.py"),
        ("oracle", task.oracle_tests, q._hidden_name(task)),
    ):
        target = evidence / name
        q._write(target, {**files, **tests})
        suites[name] = q._run(target, entry).as_dict()
    return {
        "valid": not problems and all(row["category"] == q.PASS for row in suites.values()),
        "suites": suites,
        "problems": problems,
    }


def qualify_revisions(root: Path) -> dict[str, Any]:
    rows = {}
    for task_id, revision in REVISIONS.items():
        old = task_at_revision(task_id, 1)
        task = task_at_revision(task_id, revision)
        scratch = root / task_id
        single = build_task(task, topology="single")
        multi = build_task(task, topology="multi")
        visible = {**task.seed, **task.public_tests}
        fixture = prepare_fixture(task, scratch / "fixture")
        exported = set(fixture["files_list"])
        contract_materialized = (
            single.visible_files == multi.visible_files == visible
            and fixture["matches_frozen_seed"]
            and exported == set(visible)
            and not exported.intersection(task.oracle_tests)
            and all(task.seed.get(path) != contents for path, contents in task.reference.items())
        )
        mutations = q.qualify_mutations(task, _NEW_MUTATIONS[task_id])
        qualification = q.qualify_task(task)
        equivalent = _equivalent(task, scratch / "equivalent")
        static_dag = q.static_dag_checks(task, q._merged(task))
        leak_checks = q.leak_checks(task)
        old_identity = _identity(old, 1)
        identity = _identity(task, revision)
        qualified = (
            qualification["qualified"]
            and all(row["valid"] for row in mutations)
            and equivalent["valid"]
            and contract_materialized
            and static_dag["valid"]
            and leak_checks["valid"]
        )
        rows[task_id] = {
            "historical": old_identity,
            "current": identity,
            "qualified": qualified,
            "qualification_digest": digest_of(
                {
                    "task": identity,
                    "qualification": {
                        name: qualification[name]["category"]
                        for name in (
                            "seed_public",
                            "seed_oracle",
                            "reference_public",
                            "reference_oracle",
                        )
                    },
                    "mutations": {row["mutation_id"]: row["valid"] for row in mutations},
                    "equivalent": equivalent["valid"],
                    "static_dag": static_dag["valid"],
                    "leak_checks": leak_checks["valid"],
                    "fixture": contract_materialized,
                }
            ),
            "qualification": qualification,
            "mutations": mutations,
            "equivalent": equivalent,
            "static_dag": static_dag,
            "leak_checks": leak_checks,
            "fixture": fixture,
            "same_public_contract": contract_materialized,
        }
    return {"tasks": rows, "qualified": all(row["qualified"] for row in rows.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args()
    args.evidence_root.mkdir(parents=True, exist_ok=False)
    report = qualify_revisions(args.evidence_root)
    (args.evidence_root / "qualification.json").write_text(
        json.dumps(report, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if report["qualified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
