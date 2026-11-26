# Incident 2026-11-19 — lost deliveries

## What the operators saw

- A burst of five messages against a broker with capacity three. Every `publish` call returned
  normally, but only three messages were ever handled.
- A replay of one message whose handler timed out once. The message was never delivered again and
  ended in the dead-letter list after a single attempt.
- After a process restart, a message that had been taken but not acknowledged was gone: it was in
  neither the queue nor the processed set.

## Evidence in this directory

- `incident.log` is the trace captured from `python diagnostics/reproduce.py` on this checkout.
  The burst section prints every publish result, the handled ids and the computed missing ids. The
  replay section prints the handler outcome, the counters, the acked/dead ids and the clock.
- `history.jsonl` holds two earlier deployments with the same shape: publishes accepted, handled
  count lower, ids missing.
- `reproduce.py` uses a deterministic clock and temporary state; running it twice gives the same
  trace. It does not print a fix and does not read hidden tests.

## Constraints for any repair

- `docs/INTERFACE.md` is the frozen contract.
- A message is lost if it is neither queued, in flight, acked, nor dead; that must never happen.
- The replay must be driven by the injected clock. Nothing may sleep.
