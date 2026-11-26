"""CSV export request (v1 call site behind an alias and a partial).

The audit call never names ``record`` directly, so a search for ``record(`` misses it.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import partial

from foundation.audit import record as write_audit
from foundation.models import Order

_emit = partial(write_audit)


def export(orders: Iterable[Order], *, actor: str) -> dict:
    items = list(orders)
    return _emit("export", {"rows": len(items), "format": "csv"})
