"""Return workflow (v1 call site).

The state logic is correct; only the audit calls are still positional.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from foundation.audit import record
from foundation.errors import DomainError

STATES = frozenset({"requested", "approved", "received", "refunded", "rejected"})
TRANSITIONS: dict[tuple[str, str], str] = {
    ("requested", "approve"): "approved",
    ("requested", "reject"): "rejected",
    ("approved", "receive"): "received",
    ("received", "refund"): "refunded",
}
TERMINAL = frozenset({"refunded", "rejected"})


class InvalidReturnStep(DomainError):
    """The return case is not in a state that allows this step."""


@dataclass
class ReturnCase:
    return_id: str
    order_id: str
    reason: str
    amount_minor: int
    state: str = "requested"
    history: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.return_id or not self.order_id:
            raise DomainError("a return case needs a return id and an order id")
        if not self.reason:
            raise DomainError("a return case needs a reason")
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise DomainError("amount_minor must be an integer")
        if self.amount_minor <= 0:
            raise DomainError("amount_minor must be positive")
        if self.state not in STATES:
            raise DomainError(f"unknown return state {self.state!r}")


def open_case(return_id: str, order_id: str, reason: str, amount_minor: int) -> ReturnCase:
    return ReturnCase(return_id, order_id, reason, amount_minor)


class ReturnWorkflow:
    def __init__(self, case: ReturnCase) -> None:
        self.case = case

    def _move(self, event: str) -> str:
        target = TRANSITIONS.get((self.case.state, event))
        if target is None:
            raise InvalidReturnStep(f"{event!r} is not allowed from {self.case.state!r}")
        self.case.state = target
        self.case.history.append((event, target))
        return target

    def _record(self, kind: str, state: str, actor: str):
        data = {
            "return_id": self.case.return_id,
            "order_id": self.case.order_id,
            "state": state,
            "amount": self.case.amount_minor,
        }
        return record(kind, data, actor)

    def approve(self, *, actor: str):
        return self._record("return_approved", self._move("approve"), actor)

    def reject(self, *, actor: str):
        return self._record("return_rejected", self._move("reject"), actor)

    def receive(self, *, actor: str):
        return self._record("return_received", self._move("receive"), actor)

    def refund(self, *, actor: str):
        return self._record("return_refunded", self._move("refund"), actor)
