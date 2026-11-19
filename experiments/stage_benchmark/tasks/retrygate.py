"""Task ``retrygate`` — a retry decision whose rules are spread across three coupled concerns.

Classification, attempt accounting and backoff are each simple. The decision is not: whether a retry
is allowed depends on the class *and* on how many attempts remain *and* on whether the caller supplied
a server hint, and the attempt is consumed only on the paths that schedule one. Splitting this work
between workers means splitting one rule set; the expected delegation benefit is unclear by design.
"""

from __future__ import annotations

from .base import TaskSpec, WrongVariant

TASK_ID = "retrygate"

_README = """# retrygate

Retry decisions for one call.

* `retrygate.classify` maps an error code to a retry class.
* `retrygate.budget` counts attempts.
* `retrygate.policy` decides whether to retry, after how long, and why. It is incomplete.
"""

_PYTEST = """[pytest]
testpaths = tests
"""

_INIT = '''"""Retry decisions."""

from .budget import AttemptBudget
from .classify import PERMANENT, RATE_LIMITED, TRANSIENT, UNKNOWN, classify
from .errors import PolicyError
from .policy import BACKOFF_BASE_MS, BACKOFF_CAP_MS, Decision, decide

__all__ = [
    "BACKOFF_BASE_MS",
    "BACKOFF_CAP_MS",
    "PERMANENT",
    "RATE_LIMITED",
    "TRANSIENT",
    "UNKNOWN",
    "AttemptBudget",
    "Decision",
    "PolicyError",
    "classify",
    "decide",
]
'''

_ERRORS = '''"""The policy's own failure."""

from __future__ import annotations


class PolicyError(ValueError):
    """A decision that cannot be made with the given arguments."""

    def __init__(self, message: str, code: str = "invalid_request") -> None:
        super().__init__(message)
        self.code = code
'''

_CLASSIFY = '''"""Error codes to retry classes.

An unknown code is not an error: it is :data:`UNKNOWN`, and the policy treats it conservatively.
"""

from __future__ import annotations

TRANSIENT = "transient"
PERMANENT = "permanent"
RATE_LIMITED = "rate_limited"
UNKNOWN = "unknown"

_BY_CODE: dict[str, str] = {
    "timeout": TRANSIENT,
    "connection_reset": TRANSIENT,
    "server_error": TRANSIENT,
    "auth": PERMANENT,
    "validation": PERMANENT,
    "not_found": PERMANENT,
    "rate_limited": RATE_LIMITED,
}


def classify(error: str) -> str:
    """The retry class of an error code."""

    return _BY_CODE.get(error, UNKNOWN)
'''

_BUDGET = '''"""Attempt accounting.

An attempt is consumed when a retry is scheduled, not when it is considered. A budget that is
exhausted refuses to consume and says so.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PolicyError


@dataclass
class AttemptBudget:
    max_attempts: int
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise PolicyError("max_attempts must be at least 1", code="invalid_budget")

    @property
    def remaining(self) -> int:
        return self.max_attempts - self.attempts

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def consume(self) -> int:
        """Take one attempt and return its 1-based number."""

        if self.exhausted:
            raise PolicyError("no attempts left", code="exhausted")
        self.attempts += 1
        return self.attempts
'''

_POLICY = '''"""The retry decision.

``decide(budget, error, retry_after_ms=None)`` returns a :class:`Decision`. The rules:

* a ``permanent`` error is never retried and consumes nothing;
* an exhausted budget is never retried and consumes nothing;
* a ``transient`` error is retried with exponential backoff
  ``min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_CAP_MS)`` where ``attempt`` is the number being
  scheduled;
* a ``rate_limited`` error is retried after ``retry_after_ms`` when the caller supplied one (a
  supplied ``0`` is a hint, not an absent value) and with the same backoff otherwise;
* an ``unknown`` error is retried only when at least two attempts remain, so the last attempt is
  never spent on an error the policy does not understand;
* the attempt is consumed only when the decision is to retry.

``Decision.reason`` is one of ``permanent``, ``exhausted``, ``transient``, ``rate_limited``,
``unknown`` or ``unknown_reserved`` (the unknown class refused for lack of spare attempts).
A negative ``retry_after_ms`` is an error.
"""

from __future__ import annotations

from dataclasses import dataclass

from .budget import AttemptBudget

BACKOFF_BASE_MS = 100
BACKOFF_CAP_MS = 5000


@dataclass(frozen=True)
class Decision:
    retry: bool
    delay_ms: int
    reason: str
    attempt: int


def decide(budget: AttemptBudget, error: str, retry_after_ms: int | None = None) -> Decision:
    """Whether to retry ``error``, how long to wait, and why."""

    raise NotImplementedError("decide is not implemented yet")
'''

_TEST_CLASSIFY = """from retrygate.classify import PERMANENT, RATE_LIMITED, TRANSIENT, UNKNOWN, classify


def test_timeouts_are_transient() -> None:
    assert classify("timeout") == TRANSIENT


def test_server_errors_are_transient() -> None:
    assert classify("server_error") == TRANSIENT


def test_auth_is_permanent() -> None:
    assert classify("auth") == PERMANENT


def test_rate_limiting_has_its_own_class() -> None:
    assert classify("rate_limited") == RATE_LIMITED


def test_an_unknown_code_is_unknown() -> None:
    assert classify("quota_xyz") == UNKNOWN
"""

_TEST_BUDGET = """import pytest

from retrygate.budget import AttemptBudget
from retrygate.errors import PolicyError


def test_a_fresh_budget_is_full() -> None:
    assert AttemptBudget(3).remaining == 3


def test_consuming_numbers_attempts_from_one() -> None:
    budget = AttemptBudget(3)
    assert [budget.consume(), budget.consume()] == [1, 2]


def test_exhaustion_is_reported() -> None:
    budget = AttemptBudget(1)
    budget.consume()
    assert budget.exhausted is True


def test_consuming_past_the_end_raises() -> None:
    budget = AttemptBudget(1)
    budget.consume()
    with pytest.raises(PolicyError) as error:
        budget.consume()
    assert error.value.code == "exhausted"


def test_a_zero_budget_is_rejected() -> None:
    with pytest.raises(PolicyError):
        AttemptBudget(0)
"""

_TEST_PUBLIC = """import pytest

from retrygate.budget import AttemptBudget
from retrygate.errors import PolicyError
from retrygate.policy import decide


def test_a_retry_reports_the_attempt_it_scheduled() -> None:
    decision = decide(AttemptBudget(3), "timeout")
    assert (decision.retry, decision.attempt) == (True, 1)


def test_the_first_backoff_is_the_base() -> None:
    decision = decide(AttemptBudget(3), "timeout")
    assert (decision.retry, decision.delay_ms, decision.reason) == (True, 100, "transient")


def test_the_second_backoff_doubles() -> None:
    budget = AttemptBudget(3)
    decide(budget, "timeout")
    assert decide(budget, "timeout").delay_ms == 200


def test_a_permanent_error_never_retries() -> None:
    decision = decide(AttemptBudget(3), "auth")
    assert (decision.retry, decision.delay_ms, decision.reason) == (False, 0, "permanent")


def test_a_server_hint_is_honoured() -> None:
    decision = decide(AttemptBudget(3), "rate_limited", retry_after_ms=750)
    assert (decision.delay_ms, decision.reason) == (750, "rate_limited")


def test_an_exhausted_budget_does_not_retry() -> None:
    budget = AttemptBudget(1)
    budget.consume()
    decision = decide(budget, "timeout")
    assert (decision.retry, decision.reason) == (False, "exhausted")


def test_an_unknown_error_needs_spare_attempts() -> None:
    decision = decide(AttemptBudget(2), "quota_xyz")
    assert (decision.retry, decision.reason) == (True, "unknown")


def test_a_negative_hint_is_rejected() -> None:
    with pytest.raises(PolicyError):
        decide(AttemptBudget(3), "rate_limited", retry_after_ms=-1)
"""

_REFERENCE = '''"""The retry decision.

``decide(budget, error, retry_after_ms=None)`` returns a :class:`Decision`. The rules:

* a ``permanent`` error is never retried and consumes nothing;
* an exhausted budget is never retried and consumes nothing;
* a ``transient`` error is retried with exponential backoff
  ``min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_CAP_MS)`` where ``attempt`` is the number being
  scheduled;
* a ``rate_limited`` error is retried after ``retry_after_ms`` when the caller supplied one (a
  supplied ``0`` is a hint, not an absent value) and with the same backoff otherwise;
* an ``unknown`` error is retried only when at least two attempts remain, so the last attempt is
  never spent on an error the policy does not understand;
* the attempt is consumed only when the decision is to retry.

``Decision.reason`` is one of ``permanent``, ``exhausted``, ``transient``, ``rate_limited``,
``unknown`` or ``unknown_reserved`` (the unknown class refused for lack of spare attempts).
A negative ``retry_after_ms`` is an error.
"""

from __future__ import annotations

from dataclasses import dataclass

from .budget import AttemptBudget
from .classify import PERMANENT, RATE_LIMITED, UNKNOWN, classify
from .errors import PolicyError

BACKOFF_BASE_MS = 100
BACKOFF_CAP_MS = 5000


@dataclass(frozen=True)
class Decision:
    retry: bool
    delay_ms: int
    reason: str
    attempt: int


def _backoff(attempt: int) -> int:
    return min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_CAP_MS)


def decide(budget: AttemptBudget, error: str, retry_after_ms: int | None = None) -> Decision:
    """Whether to retry ``error``, how long to wait, and why."""

    if retry_after_ms is not None and retry_after_ms < 0:
        raise PolicyError("retry_after_ms must not be negative", code="invalid_request")
    kind = classify(error)
    if kind == PERMANENT:
        return Decision(retry=False, delay_ms=0, reason="permanent", attempt=budget.attempts)
    if budget.exhausted:
        return Decision(retry=False, delay_ms=0, reason="exhausted", attempt=budget.attempts)
    if kind == UNKNOWN and budget.remaining < 2:
        return Decision(retry=False, delay_ms=0, reason="unknown_reserved", attempt=budget.attempts)
    attempt = budget.consume()
    if kind == RATE_LIMITED:
        delay = retry_after_ms if retry_after_ms is not None else _backoff(attempt)
        return Decision(retry=True, delay_ms=delay, reason="rate_limited", attempt=attempt)
    if kind == UNKNOWN:
        return Decision(retry=True, delay_ms=_backoff(attempt), reason="unknown", attempt=attempt)
    return Decision(retry=True, delay_ms=_backoff(attempt), reason="transient", attempt=attempt)
'''

_ORACLE = """import traceback

from retrygate.budget import AttemptBudget
from retrygate.errors import PolicyError
from retrygate.policy import BACKOFF_CAP_MS, decide

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def raises(kind, call, label):
    try:
        call()
    except kind:
        return
    except BaseException as error:  # noqa: BLE001
        FAILURES.append(f"{label}: raised {type(error).__name__}, expected {kind.__name__}")
        return
    FAILURES.append(f"{label}: did not raise")


try:
    # The backoff doubles and then stops at the cap.
    budget = AttemptBudget(10)
    delays = [decide(budget, "timeout").delay_ms for _ in range(9)]
    check(delays[:3] == [100, 200, 400], f"first delays are {delays[:3]}")
    check(delays[-1] == BACKOFF_CAP_MS, f"backoff does not cap: {delays[-1]}")
    check(all(delay <= BACKOFF_CAP_MS for delay in delays), "a delay exceeded the cap")

    # A refusal consumes nothing: the budget is unchanged.
    budget = AttemptBudget(3)
    decide(budget, "auth")
    check(budget.remaining == 3, "a permanent error consumed an attempt")
    check(decide(budget, "auth").attempt == 0, "a permanent refusal reports an attempt number")

    budget = AttemptBudget(1)
    budget.consume()
    decide(budget, "timeout")
    check(budget.remaining == 0, "an exhausted refusal changed the budget")

    # A supplied zero hint is a hint.
    decision = decide(AttemptBudget(3), "rate_limited", retry_after_ms=0)
    check((decision.retry, decision.delay_ms) == (True, 0), "retry_after_ms=0 was ignored")

    # The unknown reserve: two remaining retries, one remaining does not.
    check(decide(AttemptBudget(2), "quota_xyz").retry is True, "unknown refused with 2 remaining")
    check(
        decide(AttemptBudget(2), "quota_xyz").reason == "unknown",
        "unknown retry has the wrong reason",
    )
    budget = AttemptBudget(2)
    budget.consume()
    refused = decide(budget, "quota_xyz")
    check(refused.retry is False, "unknown retried with only one attempt remaining")
    check(refused.reason == "unknown_reserved", f"unknown refusal reason: {refused.reason}")
    check(budget.remaining == 1, "an unknown refusal consumed an attempt")

    # Attempt numbers are the numbers of the retries actually scheduled.
    budget = AttemptBudget(4)
    numbers = [decide(budget, "server_error").attempt for _ in range(4)]
    check(numbers == [1, 2, 3, 4], f"attempt numbers are {numbers}")

    # Rate limiting consumes a slot like any other retry.
    budget = AttemptBudget(2)
    decide(budget, "rate_limited")
    check(budget.remaining == 1, "rate limiting did not consume a slot")

    # An unknown error is not silently treated as permanent on the first attempt.
    check(decide(AttemptBudget(3), "wat").retry is True, "unknown refused with 3 remaining")

    raises(PolicyError, lambda: decide(AttemptBudget(3), "timeout", retry_after_ms=-5), "negative hint")
except BaseException:  # noqa: BLE001
    traceback.print_exc()
    print("HIDDEN_FAIL")
    raise SystemExit(1) from None

if FAILURES:
    for item in FAILURES:
        print(f"FAIL {item}")
    print("HIDDEN_FAIL")
    raise SystemExit(1)
print("HIDDEN_OK")
"""

SPEC = TaskSpec(
    task_id=TASK_ID,
    title="Retry decision under coupled rules",
    structure="COUPLED_UNCERTAIN",
    rationale=(
        "Three small modules, one decision. Classification, attempt accounting and backoff are each "
        "trivial in isolation, and the failure modes live between them: whether a refusal consumes an "
        "attempt, whether a zero hint is a hint, whether the last slot may be spent on an unknown "
        "error. A worker given only one module cannot answer any of those questions, so the expected "
        "delegation benefit is low and the coordination cost is real."
    ),
    goal=(
        "The retrygate project classifies errors and counts attempts, but retrygate/policy.py does "
        "not decide anything yet. Implement decide() so the documented retry rules hold, and verify "
        "with the public tests. Leave the classification and budget modules unchanged."
    ),
    write_scope=("retrygate/policy.py",),
    files={
        "README.md": _README,
        "pytest.ini": _PYTEST,
        "retrygate/__init__.py": _INIT,
        "retrygate/errors.py": _ERRORS,
        "retrygate/classify.py": _CLASSIFY,
        "retrygate/budget.py": _BUDGET,
        "retrygate/policy.py": _POLICY,
        "tests/test_classify.py": _TEST_CLASSIFY,
        "tests/test_budget.py": _TEST_BUDGET,
        "tests/test_public_policy.py": _TEST_PUBLIC,
    },
    reference={"retrygate/policy.py": _REFERENCE},
    wrong=(
        WrongVariant(
            name="backoff_without_cap",
            covers="a delay that grows without bound",
            file="retrygate/policy.py",
            edits=(
                (
                    "    return min(BACKOFF_BASE_MS * 2 ** (attempt - 1), BACKOFF_CAP_MS)",
                    "    return BACKOFF_BASE_MS * 2 ** (attempt - 1)",
                ),
            ),
        ),
        WrongVariant(
            name="permanent_consumes",
            covers="a refusal that still spends an attempt",
            file="retrygate/policy.py",
            edits=(
                (
                    '    if kind == PERMANENT:\n        return Decision(retry=False, delay_ms=0, reason="permanent", attempt=budget.attempts)',
                    '    if kind == PERMANENT:\n        budget.consume()\n        return Decision(retry=False, delay_ms=0, reason="permanent", attempt=budget.attempts)',
                ),
            ),
        ),
        WrongVariant(
            name="unknown_ignores_reserve",
            covers="the last attempt spent on an unknown error",
            file="retrygate/policy.py",
            edits=(
                (
                    "    if kind == UNKNOWN and budget.remaining < 2:",
                    "    if kind == UNKNOWN and budget.remaining < 1:",
                ),
            ),
        ),
        WrongVariant(
            name="zero_hint_treated_as_absent",
            covers="a supplied retry_after_ms=0 replaced by the backoff",
            file="retrygate/policy.py",
            edits=(
                (
                    "        delay = retry_after_ms if retry_after_ms is not None else _backoff(attempt)",
                    "        delay = retry_after_ms or _backoff(attempt)",
                ),
            ),
        ),
    ),
    compatibility_command=("tests/test_classify.py", "tests/test_budget.py"),
    target_command=("tests/test_public_policy.py",),
    oracle_source=_ORACLE,
    expectation={
        "compatibility_tests": 10,
        "target_tests": 8,
        "reference_tests": 18,
        "independent_investigations": 0,
        "acceptance_difficulty": "MEDIUM",
    },
    labels={
        "change_surface": "1 file, 1 function; three interacting rule sources",
        "key_dependencies": "classify(), AttemptBudget.consume/remaining, backoff formula",
        "oracle_scope": "cap, non-consuming refusals, zero hint, unknown reserve, attempt numbering",
    },
    notes="Deliberately coupled: the public suite states the happy paths only.",
)
