"""Ledger wrapper (v1).

The wrapper hides the audit API from its callers, so a broken call here breaks every ledger entry,
not just one caller.
"""

from __future__ import annotations

from foundation.audit import record


def post(kind: str, amount: int, order_id: str = "", actor: str | None = None) -> dict:
    return record(kind, {"order_id": order_id, "amount": amount})
