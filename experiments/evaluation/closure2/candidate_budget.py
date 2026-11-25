# ruff: noqa: E501
"""Closure-2 S07: the owner-approved **candidate** budget profile.

Two owner decisions moved two numbers for the S08 canary and everything after it:

* worker max turns ``12 -> 20``;
* per-cell token budget ``260_000 -> 500_000``.

Neither number can be edited where it already lives. ``protocol.BUDGET`` is inside
``preregistration_payload()``, so touching it moves ``3080f313…`` — the digest the S06 execution and
the two consumed authorizations name — and every arm digest with it. ``catalog.build_task`` is worse:
worker and task turn caps are part of ``effective_public_task_digest``, so the frozen C2-A task table
would stop matching.

This module therefore adds a **third** budget object rather than rewriting the second one:

``FROZEN_PROFILE``
    reads its numbers *from* ``protocol.BUDGET`` and refuses to exist if the frozen table moved. It is
    the identity every historical run keeps: 36 / 12 / 260 000.
``CANDIDATE_PROFILE``
    the two owner-approved values with every other knob copied unchanged. Its digest names the
    revision, and ``candidate_registration_digest()`` binds it to the frozen preregistration without
    altering it.

Nothing here is approved: the profile is a candidate until an authorization record names its digest.
The frozen registration file is not edited, the old differential is not reinterpreted, and a run that
does not name this profile digest still executes the frozen numbers.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from experiments.evaluation.closure2.protocol import (
    BUDGET,
    C2A_R1_PREREGISTRATION_DIGEST,
    PROTOCOL_REVISION,
)
from experiments.evaluation.models import canonical_digest

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = REPO_ROOT / "experiments" / "results" / "closure2" / "candidate-budget.json"

#: Candidate revision. It is inside the profile digest, so settling it is part of freezing it.
CANDIDATE_BUDGET_REVISION = "closure-2-s07-candidate-budget-1"

#: The owner decision this revision implements, quoted so the artifact carries its own provenance.
OWNER_DECISION: dict[str, Any] = {
    "decided_on": "2026-11-25",
    "worker_max_turns": 20,
    "per_cell_token_budget": 500_000,
    "replaces": {"worker_max_turns": 12, "per_cell_token_budget": 260_000},
    "authorizes": "offline preparation only",
    "does_not_authorize": [
        "any real model request",
        "any sandbox creation",
        "the S09/S11 parameter freeze",
    ],
}

#: The knobs the owner approved. Everything else must stay identical to the frozen table.
CHANGED_KNOBS: tuple[str, ...] = (
    "multi.max_turns_per_worker",
    "single.token_budget+multi.token_budget",
)


class CandidateBudgetError(RuntimeError):
    """The candidate profile does not describe the owner decision. Never downgraded to a warning."""


@dataclass(frozen=True)
class BudgetProfile:
    """One budget profile: the numbers a run admits against, and the identity of that choice."""

    revision: str
    status: str
    single_max_turns: int
    multi_worker_max_turns: int
    multi_workers: int
    cell_token_budget: int
    max_tool_calls: int
    timeout_seconds: int

    def payload(self) -> dict[str, Any]:
        """Every field that defines the profile. No timestamp, no path, no approval state."""

        return {
            "revision": self.revision,
            "status": self.status,
            "single_max_turns": self.single_max_turns,
            "multi_worker_max_turns": self.multi_worker_max_turns,
            "multi_workers": self.multi_workers,
            "cell_token_budget": self.cell_token_budget,
            "max_tool_calls": self.max_tool_calls,
            "timeout_seconds": self.timeout_seconds,
        }

    def digest(self) -> str:
        return canonical_digest(self.payload())

    def expectations(self, topology: str) -> dict[str, int]:
        """The invariants ``pilot_runner.frozen_budget`` asserts for one topology."""

        if topology == "single":
            return {
                "max_turns": self.single_max_turns,
                "token_budget": self.cell_token_budget,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.timeout_seconds,
            }
        if topology == "multi":
            return {
                "max_turns_per_worker": self.multi_worker_max_turns,
                "workers": self.multi_workers,
                "token_budget": self.cell_token_budget,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.timeout_seconds,
            }
        raise CandidateBudgetError(f"unknown topology {topology!r}")

    def agent_max_turns(self, topology: str) -> int:
        """The per-agent cap. Single keeps one agent; multi applies the cap to every worker."""

        return self.single_max_turns if topology == "single" else self.multi_worker_max_turns

    def to_budget_table(self) -> dict[str, Any]:
        """The profile in the shape of ``protocol.BUDGET``'s two topology blocks."""

        return {
            "single": {
                "max_turns": self.single_max_turns,
                "token_budget": self.cell_token_budget,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.timeout_seconds,
            },
            "multi": {
                "max_turns_per_worker": self.multi_worker_max_turns,
                "workers": self.multi_workers,
                "theoretical_worker_turns": self.multi_worker_max_turns * self.multi_workers,
                "token_budget": self.cell_token_budget,
                "max_tool_calls": self.max_tool_calls,
                "timeout_seconds": self.timeout_seconds,
            },
        }


def _frozen_profile() -> BudgetProfile:
    """The frozen table, read rather than retyped, so this module cannot silently disagree."""

    single = BUDGET["single"]
    multi = BUDGET["multi"]
    return BudgetProfile(
        revision=PROTOCOL_REVISION,
        status="FROZEN",
        single_max_turns=int(single["max_turns"]),
        multi_worker_max_turns=int(multi["max_turns_per_worker"]),
        multi_workers=int(multi["workers"]),
        cell_token_budget=int(single["token_budget"]),
        max_tool_calls=int(single["max_tool_calls"]),
        timeout_seconds=int(single["timeout_seconds"]),
    )


#: The identity every executed Closure-2 run so far carries: 36 / 12 / 260 000.
FROZEN_PROFILE = _frozen_profile()

#: The owner-approved candidate: same everything, except the two approved knobs.
CANDIDATE_PROFILE = BudgetProfile(
    revision=CANDIDATE_BUDGET_REVISION,
    status="CANDIDATE_NOT_FROZEN_NOT_AUTHORIZED",
    single_max_turns=FROZEN_PROFILE.single_max_turns,
    multi_worker_max_turns=int(OWNER_DECISION["worker_max_turns"]),
    multi_workers=FROZEN_PROFILE.multi_workers,
    cell_token_budget=int(OWNER_DECISION["per_cell_token_budget"]),
    max_tool_calls=FROZEN_PROFILE.max_tool_calls,
    timeout_seconds=FROZEN_PROFILE.timeout_seconds,
)

#: Digest-addressed lookup. A plan stores the digest; the dispatcher resolves it here and fails
#: closed on an unknown value, so a plan cannot name a budget it did not get approved with.
PROFILES_BY_DIGEST: dict[str, BudgetProfile] = {
    FROZEN_PROFILE.digest(): FROZEN_PROFILE,
    CANDIDATE_PROFILE.digest(): CANDIDATE_PROFILE,
}


class UnknownBudgetProfile(CandidateBudgetError):
    """A run named a profile digest nothing defines."""


def profile_for_digest(digest: str) -> BudgetProfile:
    try:
        return PROFILES_BY_DIGEST[digest]
    except KeyError:
        raise UnknownBudgetProfile(
            f"budget profile digest {digest!r} is not defined; known digests are "
            f"{sorted(PROFILES_BY_DIGEST)}"
        ) from None


def candidate_registration_digest() -> str:
    """A **new** identity for the candidate configuration.

    It is not a re-freeze: it does not recompute ``preregistration_payload()`` and it does not replace
    ``C2A_R1_PREREGISTRATION_DIGEST``. It answers one question only — "which frozen definition, under
    which budget profile" — so an approval of this candidate cannot be read as an approval of the
    frozen 12 / 260 000 registration, and the reverse.
    """

    return canonical_digest(
        {
            "kind": "closure2_candidate_registration",
            "preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
            "budget_profile_digest": CANDIDATE_PROFILE.digest(),
            "budget_profile_revision": CANDIDATE_PROFILE.revision,
        }
    )


#: Derived values, not knobs: they move because a knob moved, and reporting them as decisions would
#: overstate how many parameters the owner changed.
DERIVED_KEYS: tuple[str, ...] = ("multi.theoretical_worker_turns",)


def changed_knobs(*, left: BudgetProfile, right: BudgetProfile) -> tuple[str, ...]:
    """The dotted knob names where two profiles disagree. Used to prove only the approved two moved."""

    def flatten(profile: BudgetProfile) -> dict[str, Any]:
        table = profile.to_budget_table()
        flat: dict[str, Any] = {}
        for topology, block in table.items():
            for key, value in block.items():
                flat[f"{topology}.{key}"] = value
        return flat

    left_flat, right_flat = flatten(left), flatten(right)
    return tuple(
        sorted(
            key
            for key in left_flat
            if key not in DERIVED_KEYS and left_flat[key] != right_flat.get(key)
        )
    )


def check_candidate() -> list[str]:
    """Everything the candidate must satisfy before it can be proposed for approval."""

    problems: list[str] = []
    # The frozen profile must still be the protocol's own table: a drift there invalidates both.
    if FROZEN_PROFILE.to_budget_table()["single"]["max_turns"] != int(
        BUDGET["single"]["max_turns"]
    ):
        problems.append("frozen profile single.max_turns drifted from protocol.BUDGET")
    if FROZEN_PROFILE.to_budget_table()["multi"]["max_turns_per_worker"] != int(
        BUDGET["multi"]["max_turns_per_worker"]
    ):
        problems.append("frozen profile multi.max_turns_per_worker drifted from protocol.BUDGET")
    if FROZEN_PROFILE.cell_token_budget != int(BUDGET["multi"]["token_budget"]):
        problems.append("frozen profile token budget drifted from protocol.BUDGET")
    # Exactly the approved knobs, and nothing else.
    if CANDIDATE_PROFILE.single_max_turns != FROZEN_PROFILE.single_max_turns:
        problems.append("the candidate changed the single-agent turn cap, which was not approved")
    if CANDIDATE_PROFILE.multi_worker_max_turns != int(OWNER_DECISION["worker_max_turns"]):
        problems.append("the candidate worker turn cap is not the owner-approved 20")
    if CANDIDATE_PROFILE.cell_token_budget != int(OWNER_DECISION["per_cell_token_budget"]):
        problems.append("the candidate cell token budget is not the owner-approved 500000")
    for key in ("multi_workers", "max_tool_calls", "timeout_seconds"):
        if getattr(CANDIDATE_PROFILE, key) != getattr(FROZEN_PROFILE, key):
            problems.append(f"the candidate changed {key}, which was not approved")
    # The whole point: the frozen identity must be untouched by defining a candidate.
    if FROZEN_PROFILE.revision != PROTOCOL_REVISION:
        problems.append("the frozen profile is not the current protocol revision")
    if CANDIDATE_PROFILE.digest() == FROZEN_PROFILE.digest():
        problems.append("the candidate has the same digest as the frozen profile")
    return problems


def candidate_payload() -> dict[str, Any]:
    """The offline artifact: the revision record, both profiles and what stays unfrozen."""

    changed = changed_knobs(left=FROZEN_PROFILE, right=CANDIDATE_PROFILE)
    return {
        "kind": "closure2_candidate_budget_revision",
        "stage": "closure-2-s07",
        "status": "CANDIDATE_NOT_FROZEN_NOT_AUTHORIZED",
        "revision": CANDIDATE_BUDGET_REVISION,
        "owner_decision": dict(OWNER_DECISION),
        "supersedes_for_future_stages_only": {
            "protocol_revision": FROZEN_PROFILE.revision,
            "preregistration_digest": C2A_R1_PREREGISTRATION_DIGEST,
            "budget": FROZEN_PROFILE.to_budget_table(),
            "note": (
                "the frozen registration, its digest and every historical run keep the numbers they "
                "were executed with; this revision is not applied retroactively"
            ),
        },
        "frozen_profile": {
            **FROZEN_PROFILE.payload(),
            "digest": FROZEN_PROFILE.digest(),
        },
        "candidate_profile": {
            **CANDIDATE_PROFILE.payload(),
            "digest": CANDIDATE_PROFILE.digest(),
        },
        "candidate_registration_digest": candidate_registration_digest(),
        "changed_knobs": list(changed),
        "expected_changed_knobs": list(CHANGED_KNOBS),
        "unchanged_knobs": [
            key
            for key in (
                "single.max_turns",
                "multi.workers",
                "single.max_tool_calls",
                "multi.max_tool_calls",
                "single.timeout_seconds",
                "multi.timeout_seconds",
            )
        ],
        "budget_sharing": {
            "mechanism": "atlasos.budget.RunBudgetState(limit_tokens=token_budget)",
            "scope": "one budget per cell/run; every agent of the run draws from it",
            "covers": ["the single agent", "every worker of a multi-worker cell"],
            "turn_caps": "per agent, not shared: 36 for the single agent, 20 for each worker",
            "multi_theoretical_turns": (
                f"{CANDIDATE_PROFILE.multi_worker_max_turns} x "
                f"{CANDIDATE_PROFILE.multi_workers} agents = "
                f"{CANDIDATE_PROFILE.multi_worker_max_turns * CANDIDATE_PROFILE.multi_workers} turns, "
                f"inside one {CANDIDATE_PROFILE.cell_token_budget:,} token budget"
            ),
            "enforcement": {
                "atlas": "refused before send when the reservation would breach the budget",
                "native": "no in-run guard; the same threshold is applied after the run",
            },
        },
        "six_arm_comparability": {
            "single_arms": ["N0", "A0"],
            "multi_arms": ["N1", "A1-L", "A1-P", "A2-P"],
            "single": {
                "max_turns_per_agent": CANDIDATE_PROFILE.single_max_turns,
                "token_budget": CANDIDATE_PROFILE.cell_token_budget,
                "note": "the single-agent cap was not part of the owner decision and did not move",
            },
            "multi": {
                "max_turns_per_worker": CANDIDATE_PROFILE.multi_worker_max_turns,
                "workers": CANDIDATE_PROFILE.multi_workers,
                "token_budget": CANDIDATE_PROFILE.cell_token_budget,
                "note": (
                    "the token budget is identical in both topologies, so the single arm gets "
                    "exactly the same admission budget as a multi arm and never less"
                ),
            },
        },
        "not_authorized": (
            "this record authorizes no execution: it is a candidate configuration, and every plan "
            "that would use it still needs its own approval naming its own digest"
        ),
        "contract_problems": check_candidate(),
        "model_calls": 0,
        "sandbox_runs": 0,
    }


def write_candidate_record(path: Path) -> dict[str, Any]:
    payload = candidate_payload()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "closure-2 candidate budget revision: offline record only. "
            "This writes a revision; it never starts a run."
        )
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args(argv)
    payload = write_candidate_record(Path(args.out))
    print(
        json.dumps(
            {
                "status": payload["status"],
                "revision": payload["revision"],
                "contract_problems": payload["contract_problems"],
                "frozen_digest": payload["frozen_profile"]["digest"],
                "candidate_digest": payload["candidate_profile"]["digest"],
                "candidate_registration_digest": payload["candidate_registration_digest"],
                "changed_knobs": payload["changed_knobs"],
                "model_calls": 0,
                "sandbox_runs": 0,
                "out": str(args.out),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not payload["contract_problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CANDIDATE_BUDGET_REVISION",
    "CANDIDATE_PROFILE",
    "CHANGED_KNOBS",
    "FROZEN_PROFILE",
    "OWNER_DECISION",
    "PROFILES_BY_DIGEST",
    "BudgetProfile",
    "CandidateBudgetError",
    "UnknownBudgetProfile",
    "candidate_payload",
    "candidate_registration_digest",
    "changed_knobs",
    "check_candidate",
    "profile_for_digest",
    "write_candidate_record",
]
