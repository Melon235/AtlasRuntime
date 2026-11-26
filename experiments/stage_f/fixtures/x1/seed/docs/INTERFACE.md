# Order platform — frozen v2 audit contract

`foundation/audit.py` is the shared audit API. It was migrated to v2 and is frozen. The subsystems
below still call the v1 surface, and the migration is not finished.

`foundation/audit.py`, `foundation/errors.py`, `foundation/money.py`, `foundation/models.py`,
`catalog/pricing.py`, `catalog/discounts.py`, `billing/tax.py`, `returns/policy.py`,
`inventory/stock.py`, `events/bus.py`, `notifications/templates.py` are frozen: they are already
correct and must not be edited.

## The frozen API

```
Receipt(kind: str, data: dict, actor: str | None)      # frozen dataclass
record(*, kind: str, data: dict, actor: str | None = None) -> Receipt
```

- `kind` must be a non-empty string, `data` a dict, `actor` `None` or a non-empty string; anything
  else is a `ValueError`.
- `record` returns a `Receipt` whose `data` is a copy of the input dict.
- The v1 positional form `record(kind, data)` no longer runs.

## Money and orders

`foundation.money.Money(minor, currency)` holds integer minor units. `to_minor(money)` returns the
integer, `parse_minor(text)` converts a decimal string strictly (at most two fraction digits), and
`format_minor(minor, currency)` renders it. `foundation.models.Order.total` is a `Money`.

## Subsystem contracts

Every function below accepts `actor` as a keyword-only argument and returns the `Receipt` unless
stated otherwise.

| Function | Kind | `data` |
|---|---|---|
| `billing.charge.charge(order, *, actor)` | `charge` | `{"order_id": str, "amount": int, "currency": str, "tax": int}` |
| `billing.refund.refund(order, *, actor)` | `refund` | same shape; `amount` is negative |
| `billing.ledger.post(kind, amount, *, actor, order_id)` | the given `kind` | `{"order_id": str, "amount": int}` |
| `billing.settlement.settle(orders, *, actor)` | `settlement` per order | as `ledger.post`; returns `list[Receipt]` |
| `billing.reconciliation.reconcile(charges, refunds, *, actor)` | `reconciliation` | `{"charged": int, "refunded": int, "net": int}` |
| `reporting.daily.daily(orders, *, actor)` | `daily` | `{"date": str, "orders": int, "total": int}` |
| `reporting.monthly.monthly(orders, *, actor)` | `monthly` | `{"month": str, "orders": int, "total": int, "currencies": list[str]}` |
| `reporting.export.export(orders, *, actor)` | `export` | `{"rows": int, "format": "csv", "total": int}` |
| `returns.service.request_return(order, *, actor, delivered_on, today, reason="changed_mind")` | `return` | `{"order_id": str, "reason": str, "fee": int, "refundable": int}` |
| `returns.workflow.ReturnWorkflow.approve/reject/receive/refund(actor=...)` | `return_approved` / `return_rejected` / `return_received` / `return_refunded` | `{"return_id": str, "order_id": str, "state": str, "amount": int}` |
| `inventory.reservations.reserve(order, *, actor, stock)` | `reservation` | `{"order_id": str, "units": int, "skus": list[str]}` |
| `inventory.transfers.transfer(stock, *, actor, from_sku, to_sku, units)` | `transfer` | `{"from": str, "to": str, "units": int}` |
| `billing.statements.statement(orders, *, actor)` | `statement` | `{"orders": int, "gross": int, "credits": int, "net": int}` |
| `events.subscribers.audit_subscriber(event)` | `bus` | `{"channel": str, "count": int}` |
| `notifications.service.Notifier.send(order, *, actor, channel="email")` | `notification` | `{"order_id": str, "channel": str, "length": int}` |

Rules that follow from the table:

- `amount` is `to_minor(order.total)`; the charge `tax` is `(amount * 20) // 100`.
- A refund amount is `-to_minor(order.total)` and the refund tax is `-(tax of the total)`.
- `ledger.post` rejects a zero `amount` with `ValueError` and never emits an event for it.
- `settle` posts one `settlement` entry per order through `ledger.post`, in input order.
- `reconcile` sums `data["amount"]` over charge receipts and the absolute value of the refund
  receipts; `net = charged - refunded`.
- `daily` uses the earliest `placed_on` in the input (`""` when there are no orders) and the sum of
  `to_minor(order.total)`.
- `monthly` uses the `YYYY-MM` prefix of the earliest `placed_on` (`""` when there are no orders) and
  a sorted list of the distinct order currencies.
- `export` always reports `format="csv"`; the workspace does not need to render a file.
- `audit_subscriber` receives `{"channel": str, "payload": list, "actor": str | None}` and emits
  `count = len(payload)` and `actor` from the event.
- `Notifier.send` calls its injected `sink` (the frozen `record`) with keyword arguments and reports
  the rendered template length.
- `request_return` first checks the frozen 30-day window (`returns.policy.within_window`); a closed
  window raises `DomainError`. The fee comes from `returns.policy.restocking_fee`, `refundable` from
  `returns.policy.refundable`, both in minor units.
- `ReturnWorkflow` follows `requested --approve|reject--> approved|rejected`,
  `approved --receive--> received`, `received --refund--> refunded`; any other step is an
  `InvalidReturnStep`, and `refunded` / `rejected` are terminal. Every step returns the receipt of
  its own event and records the step in `case.history`.
- `reserve` is all-or-nothing: after an `InsufficientStock` failure the stock ledger is unchanged.
  `units` is the sum of the line quantities, not the number of lines, and `skus` is sorted.
- `transfer` needs two different SKUs and a positive unit count; it reserves the source before
  releasing the target and records one `transfer` event. An unknown source shortage is an
  `InsufficientStock` and leaves both SKUs unchanged.
- `statement` sums gross order totals and the `WELCOME10` discount per order; `net = gross - credits`.
- `search/index.py` is already migrated and must keep emitting kind `search` with
  `{"term": str}`; do not change its behaviour.
- Every function accepts `actor` and forwards it; a positional call to `record` is a defect even if
  it happens to run.
