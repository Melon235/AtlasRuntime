"""Stage C1-B: the failure classifier names only what the runtime recorded.

The C1 run stopped a dispatch with ``UNKNOWN_FAILURE_KIND`` while the runtime had recorded
``main.stop_reason = "budget_exceeded"`` and the budget's own rejection message. These tests pin
both directions: the recorded stops are named, and everything else stays unclassified so the audit
still fails closed.
"""

from __future__ import annotations

from typing import Any

from atlasos.app.main_driven import CLASSIFIED_FAILURE_KINDS, classify_failure

BUDGET_MESSAGE = "budget_exhausted: worker atlas-main needs ~19650 tokens (used 492647, ...)"


class Stop:
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        self.message = message


class Result:
    def __init__(self, code: str | None = None, *, success: bool = False) -> None:
        self.success = success
        self.stop_reason = Stop(code, f"stop {code}") if code else None


class Budget:
    def __init__(self, stop_reason: str | None = None) -> None:
        self._stop = stop_reason

    def as_dict(self) -> dict[str, Any]:
        return {"stop_reason": self._stop, "used_tokens": 492647, "limit_tokens": 500000}


def test_a_budget_rejection_is_budget_exhaustion() -> None:
    record = classify_failure(
        failure_text=None,
        main_result=Result("budget_exceeded", success=False),
        acceptance_verdict="NOT_CONFIGURED",
        budget_state=Budget(BUDGET_MESSAGE),
        succeeded=False,
    )

    assert record is not None
    assert record["kind"] == "budget_exhaustion"
    assert record["classified_from"] == "recorded_stop_evidence"
    assert record["main_stop_reason"] == "budget_exceeded"
    assert record["budget_stop_reason"] == BUDGET_MESSAGE
    assert "budget_exhausted" in record["message"]


def test_the_turn_cap_is_main_max_turns() -> None:
    record = classify_failure(
        failure_text=None,
        main_result=Result("max_turns"),
        acceptance_verdict="NOT_CONFIGURED",
        budget_state=Budget(),
        succeeded=False,
    )

    assert record is not None
    assert record["kind"] == "main_max_turns"


def test_a_failed_acceptance_is_named_but_a_bare_failure_is_not() -> None:
    acceptance = classify_failure(
        failure_text=None,
        main_result=Result(None, success=True),
        acceptance_verdict="FAIL",
        budget_state=None,
        succeeded=False,
    )
    assert acceptance is not None
    assert acceptance["kind"] == "acceptance_failed"


def test_an_exception_stays_unclassified() -> None:
    record = classify_failure(
        failure_text="sandbox provider 'opensandbox' is unavailable",
        main_result=Result(None),
        acceptance_verdict="NOT_CONFIGURED",
        budget_state=None,
        succeeded=False,
    )

    assert record is not None
    assert record["kind"] is None
    assert record["classified_from"] == "unclassified"
    assert "unavailable" in record["message"]


def test_an_unrecognised_stop_stays_unclassified() -> None:
    record = classify_failure(
        failure_text=None,
        main_result=Result("some_unrecorded_stop"),
        acceptance_verdict="NOT_CONFIGURED",
        budget_state=None,
        succeeded=False,
    )

    assert record is not None
    assert record["kind"] is None
    assert record["main_stop_reason"] == "some_unrecorded_stop"


def test_a_provider_stop_is_named_provider_error() -> None:
    """Upstream's exhausted-retry stop and a journaled adapter failure are the same event class."""

    for code in ("provider_unavailable", "provider_error"):
        record = classify_failure(
            failure_text=None,
            main_result=Result(code),
            acceptance_verdict="NOT_CONFIGURED",
            budget_state=None,
            succeeded=False,
        )
        assert record is not None
        assert record["kind"] == "provider_error"
        assert record["classified_from"] == "recorded_stop_evidence"

    journaled = classify_failure(
        failure_text=None,
        main_result=Result(None, success=True),
        acceptance_verdict="NOT_CONFIGURED",
        budget_state=None,
        succeeded=False,
        provider_error={"exception_type": "ProviderError", "message": "upstream refused"},
    )
    assert journaled is not None
    assert journaled["kind"] == "provider_error"
    assert "upstream refused" in journaled["message"]


def test_a_successful_run_has_no_failure_record() -> None:
    assert (
        classify_failure(
            failure_text=None,
            main_result=Result(None, success=True),
            acceptance_verdict="PASS",
            budget_state=Budget(),
            succeeded=True,
        )
        is None
    )


def test_the_classifier_only_names_the_four_recorded_kinds() -> None:
    assert set(CLASSIFIED_FAILURE_KINDS) == {
        "budget_exhaustion",
        "main_max_turns",
        "acceptance_failed",
        "provider_error",
    }
