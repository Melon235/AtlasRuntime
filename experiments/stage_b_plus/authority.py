"""Stage B+ authority contract: the exact thing an Owner signature has to bind.

The previous revision had a four-field check (status, protocol digest, approver, ceiling). That is
not an authorization: it does not say *which commit*, *which cells*, *which order*, *how much per
cell*, or *whether this is the canary or the matrix*. This module replaces it.

Three properties are deliberate:

* **Scope is a first-class field.** ``CANARY_ONLY`` and ``FORMAL_MATRIX_ONLY`` are different
  authorizations. A canary signature cannot dispatch the matrix and a matrix signature cannot
  dispatch the canary, even if every digest matches.
* **The cell list is exact.** The record carries the ordered cell ids it authorizes, and they must
  equal the plan's cells in order. A missing cell, an extra cell or a reordering is a refusal.
* **The approvals cannot refer to themselves.** ``approval_provenance`` names an *external* artefact
  (a signed file, or an earlier commit) whose bytes are re-hashed here. An approval cannot name the
  commit that contains it, so there is no fixed-point to chase.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from experiments.stage_b_plus import contract

AUTHORITY_VERSION = "bplus-authority-1.0"

#: Both scopes, and the fields whose absence makes a record unsigned rather than merely incomplete.
REQUIRED_SIGNATURE_FIELDS: tuple[str, ...] = (
    "approved_by",
    "approved_at",
    "owner_statement",
    "approval_provenance",
)

REQUIRED_IDENTITY_FIELDS: tuple[str, ...] = (
    "reviewed_head",
    "execution_head",
    "executor_digest",
    "protocol_digest",
    "plan_digest",
    "plan_id",
    "scope",
)

BUDGET_ENVELOPE_FIELDS: tuple[str, ...] = ("cost_stop_usd", "per_cell_cost_stop_usd")


def canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def digest_of(payload: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def protocol_digest(document: Mapping[str, Any]) -> str:
    """The protocol digest: canonical JSON of the document, so key order cannot move it."""

    return digest_of(document)


def _finite_positive(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) > 0.0


def _git(repo_root: Path, *arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - environment
        return 1, ""
    return completed.returncode, completed.stdout.strip()


def _is_ancestor(repo_root: Path, commit: str, head: str) -> bool:
    code, _ = _git(repo_root, "merge-base", "--is-ancestor", commit, head)
    return code == 0


# -- records ----------------------------------------------------------------------------------


def plan_facts(plan: Mapping[str, Any]) -> dict[str, Any]:
    """What a signature has to be checked against, taken from the plan itself.

    The budget digest is computed from the budget the plan declares, not from the frozen default.
    For a plan that binds no budget (Stage B+ canary/formal, Stage C1, Stage C1-B) those are the
    same record, so their digest is unchanged; Stage C1-C binds its own and a signed record carrying
    the 500,000-token digest is refused by the comparison below.
    """

    cells = plan["cells"]
    declared = plan.get("budget")
    budget = dict(declared) if isinstance(declared, Mapping) and declared else dict(contract.BUDGET)
    return {
        "plan_id": plan["plan_id"],
        "scope": plan["scope"],
        "plan_digest": plan["digest"],
        "cells_count": len(cells),
        "cell_ids": [cell["cell_id"] for cell in cells],
        "cell_order": [cell["cell_id"] for cell in cells],
        "arms": list(plan["arms"]),
        "tasks": list(plan["tasks"]),
        "repetitions": int(plan["repetitions"]),
        "budget_digest": digest_of(budget),
        "token_budget": int(budget["token_budget"]),
        "nominal_token_capacity": int(plan["nominal_token_capacity"]),
    }


def pending_record(
    plan: Mapping[str, Any],
    *,
    protocol_digest_value: str,
    executor: Mapping[str, Any],
) -> dict[str, Any]:
    """A complete, unsigned record: every field present, every signature field ``None``.

    Present-but-null is the point. An Owner fills values in an existing skeleton instead of writing
    a new document, and the verifier can list exactly which fields are missing.
    """

    facts = plan_facts(plan)
    return {
        "authority_version": AUTHORITY_VERSION,
        "stage": contract.STAGE_ID,
        "protocol_version": contract.PROTOCOL_VERSION,
        "status": "PENDING_OWNER_APPROVAL",
        "authorizes_live_execution": False,
        "scope": facts["scope"],
        "plan_id": facts["plan_id"],
        "plan_digest": facts["plan_digest"],
        "protocol_digest": protocol_digest_value,
        "reviewed_head": None,
        "execution_head": None,
        "executor_digest": executor.get("executor_digest"),
        "executor_version": executor.get("executor_version"),
        "cells_count": facts["cells_count"],
        "cell_ids": facts["cell_ids"],
        "cell_order": facts["cell_order"],
        "arms": facts["arms"],
        "tasks": facts["tasks"],
        "repetitions": facts["repetitions"],
        "budget_digest": facts["budget_digest"],
        "nominal_token_capacity": facts["nominal_token_capacity"],
        "cost_stop_usd": None,
        "per_cell_cost_stop_usd": None,
        "token_stop": facts["nominal_token_capacity"],
        "attempt_limit": 1,
        "owner_statement": None,
        "approved_by": None,
        "approved_at": None,
        "approval_provenance": None,
        "reuses_s06_or_s08_balance": False,
        "notes": (
            "This record authorizes nothing. A live dispatch requires every null field filled, "
            "both cost fields to be finite and positive, and the provenance to name an external "
            "artefact whose bytes still hash to the values recorded here."
        ),
    }


def sign(
    record: Mapping[str, Any],
    *,
    approved_by: str,
    approved_at: str,
    owner_statement: str,
    reviewed_head: str,
    execution_head: str,
    cost_stop_usd: float,
    per_cell_cost_stop_usd: float,
    provenance: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a signed copy. Used by tests and by the Owner's tooling, never by this stage."""

    signed = dict(record)
    signed.update(
        {
            "status": "APPROVED",
            "authorizes_live_execution": True,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "owner_statement": owner_statement,
            "reviewed_head": reviewed_head,
            "execution_head": execution_head,
            "cost_stop_usd": cost_stop_usd,
            "per_cell_cost_stop_usd": per_cell_cost_stop_usd,
            "approval_provenance": dict(provenance),
        }
    )
    return signed


# -- verification -----------------------------------------------------------------------------


def verify(
    record: Mapping[str, Any] | None,
    *,
    plan: Mapping[str, Any],
    head: str | None,
    executor: Mapping[str, Any],
    protocol_digest_value: str,
    repo_root: Path | None = None,
    claim_consumed: bool = False,
) -> dict[str, Any]:
    """Every check a dispatch needs. Returns the problems, not a boolean."""

    problems: list[str] = []
    facts = plan_facts(plan)
    active: Mapping[str, Any] = record or {}

    if not active:
        return {
            "ready": False,
            "problems": ["no authorization record"],
            "scope": facts["scope"],
            "plan_digest": facts["plan_digest"],
            "cell_ids": facts["cell_ids"],
        }

    if active.get("authority_version") != AUTHORITY_VERSION:
        problems.append(f"authority_version is {active.get('authority_version')!r}")
    if active.get("status") != "APPROVED":
        problems.append(f"status is {active.get('status')!r}, not 'APPROVED'")
    if active.get("authorizes_live_execution") is not True:
        problems.append("authorizes_live_execution is not true")

    for field in REQUIRED_SIGNATURE_FIELDS:
        if not active.get(field):
            problems.append(f"{field} is empty")

    # identity
    if active.get("scope") != facts["scope"]:
        problems.append(
            f"record scope is {active.get('scope')!r}; this plan is {facts['scope']!r}"
        )
    if active.get("plan_id") != facts["plan_id"]:
        problems.append(f"record plan_id is {active.get('plan_id')!r}, not {facts['plan_id']!r}")
    for field, expected in (
        ("plan_digest", facts["plan_digest"]),
        ("protocol_digest", protocol_digest_value),
        ("executor_digest", executor.get("executor_digest")),
        ("budget_digest", facts["budget_digest"]),
    ):
        if active.get(field) != expected:
            problems.append(f"{field} is {active.get(field)!r}, expected {expected!r}")

    if head is None:
        problems.append("the actual execution head is unknown")
    else:
        for field in ("reviewed_head", "execution_head"):
            if active.get(field) != head:
                problems.append(f"{field} is {active.get(field)!r}, actual head is {head!r}")

    # cells: exact list, exact order, no duplicates, no missing, no extra
    authorized = list(active.get("cell_ids") or [])
    expected_cells = facts["cell_ids"]
    if len(set(authorized)) != len(authorized):
        problems.append("the authorized cell list contains duplicates")
    missing = [cell for cell in expected_cells if cell not in authorized]
    extra = [cell for cell in authorized if cell not in expected_cells]
    if missing:
        problems.append(f"the record does not authorize {len(missing)} planned cell(s)")
    if extra:
        problems.append(f"the record authorizes {len(extra)} cell(s) outside the plan")
    if authorized != expected_cells:
        problems.append("the authorized cell order differs from the plan's execution order")
    if active.get("cells_count") != facts["cells_count"]:
        problems.append(
            f"cells_count is {active.get('cells_count')!r}, plan has {facts['cells_count']}"
        )
    if list(active.get("arms") or []) != facts["arms"]:
        problems.append("the authorized arm set differs from the plan")
    if list(active.get("tasks") or []) != facts["tasks"]:
        problems.append("the authorized task set differs from the plan")

    # money
    for field in BUDGET_ENVELOPE_FIELDS:
        if not _finite_positive(active.get(field)):
            problems.append(f"{field} is {active.get(field)!r}; it must be finite and positive")
    cumulative, per_cell = active.get("cost_stop_usd"), active.get("per_cell_cost_stop_usd")
    if (
        _finite_positive(cumulative)
        and _finite_positive(per_cell)
        and float(per_cell) > float(cumulative)
    ):
        problems.append("the per-cell cost stop is larger than the cumulative stop")
    token_stop = active.get("token_stop")
    if isinstance(token_stop, bool) or not isinstance(token_stop, int) or token_stop <= 0:
        problems.append(f"token_stop is {token_stop!r}; it must be a positive integer")
    if active.get("reuses_s06_or_s08_balance") is not False:
        problems.append("the record does not state that no earlier budget remainder is reused")

    # one attempt, one dispatch
    if active.get("attempt_limit") != 1:
        problems.append(f"attempt_limit is {active.get('attempt_limit')!r}, not 1")
    if claim_consumed:
        problems.append("the dispatch claim for this plan is already consumed")

    problems.extend(_provenance_problems(active.get("approval_provenance"), repo_root=repo_root))

    return {
        "ready": not problems,
        "problems": problems,
        "scope": facts["scope"],
        "plan_id": facts["plan_id"],
        "plan_digest": facts["plan_digest"],
        "cell_ids": facts["cell_ids"],
        "authority_digest": digest_of(dict(active)),
    }


def _provenance_problems(
    provenance: Mapping[str, Any] | None, *, repo_root: Path | None
) -> list[str]:
    """An approval must name something outside itself, and its bytes are re-hashed here."""

    if not provenance:
        return ["approval_provenance is empty"]
    problems: list[str] = []
    kind = provenance.get("kind")
    if kind not in {"external_file", "commit"}:
        return [f"approval_provenance.kind is {kind!r}"]
    body = provenance.get("body")
    if not isinstance(body, Mapping):
        problems.append("approval_provenance.body is missing")
    else:
        if provenance.get("content_digest") != digest_of(dict(body)):
            problems.append("approval_provenance.content_digest does not match its body")

    if kind == "external_file":
        path = provenance.get("path")
        expected = provenance.get("sha256")
        if not path or not expected:
            problems.append("an external_file provenance needs path and sha256")
            return problems
        target = Path(str(path))
        if not target.is_absolute() and repo_root is not None:
            target = repo_root / target
        if not target.is_file():
            problems.append(f"approval file {path!r} does not exist")
            return problems
        actual = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != expected:
            problems.append(f"approval file bytes hash to {actual}, record says {expected}")
    else:
        commit = provenance.get("commit")
        if not commit:
            problems.append("a commit provenance needs a commit")
            return problems
        if repo_root is None:
            return problems
        head = provenance.get("head")
        if head and not _is_ancestor(repo_root, str(commit), str(head)):
            problems.append(f"approval commit {commit} is not an ancestor of {head}")
        # ``^{commit}`` peels an annotated tag; the braces must be doubled in an f-string.
        code, resolved = _git(repo_root, "rev-parse", f"{commit}^{{commit}}")
        if code != 0 or not resolved:
            problems.append(f"approval commit {commit} is not resolvable")
        elif head and resolved == head:
            problems.append(
                "the approval commit is the execution head itself, so the signature would have to "
                "predate the commit that contains it"
            )
    return problems


def require(
    record: Mapping[str, Any] | None,
    *,
    plan: Mapping[str, Any],
    head: str | None,
    executor: Mapping[str, Any],
    protocol_digest_value: str,
    repo_root: Path | None = None,
    claim_consumed: bool = False,
) -> dict[str, Any]:
    verdict = verify(
        record,
        plan=plan,
        head=head,
        executor=executor,
        protocol_digest_value=protocol_digest_value,
        repo_root=repo_root,
        claim_consumed=claim_consumed,
    )
    if not verdict["ready"]:
        raise PermissionError(
            f"Stage B+ {plan['scope']} is not authorized: " + "; ".join(verdict["problems"])
        )
    return verdict


__all__ = [
    "AUTHORITY_VERSION",
    "canonical",
    "digest_of",
    "pending_record",
    "plan_facts",
    "protocol_digest",
    "require",
    "sign",
    "verify",
]
