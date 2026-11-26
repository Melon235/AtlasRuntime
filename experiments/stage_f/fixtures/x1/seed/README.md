# Order platform

A small order platform: pricing, billing, reporting, an event bus, notifications and an audit trail.

`foundation/audit.py` was migrated to a keyword-only v2 API returning a `Receipt`. The subsystems in
this checkout were written against v1 and only part of the migration is done. `docs/INTERFACE.md` is
the frozen contract.

Run the public checks with `python -m pytest -q test_public.py`.

## Layout

- `foundation/` — money, orders and the frozen audit API.
- `catalog/` — price book and discount rules.
- `billing/` — tax, charge, refund, ledger wrapper, settlement, reconciliation.
- `reporting/` — daily and monthly summaries, CSV export request.
- `events/` — the in-process bus and its audit subscriber.
- `notifications/` — template rendering and the injected notifier.
- `app/` — wiring.
- `search/` — already migrated; a regression reference.
