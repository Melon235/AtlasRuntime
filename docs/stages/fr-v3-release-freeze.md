# F-R v3 release freeze

The production plan selects revision 1 for S1, S2, P1 and C2. It selects revision 2 for P2, X1,
X2 and C1. The checked-in qualification receipts preserve the accepted C1 and X2 results and
bind the new P2 and X1 results to their task identities.

- P2 preserves the full v1 JSON identifier string, including empty strings and whitespace.
  Missing IDs decode to an empty string. Non-string IDs raise `ProtocolError` and v1 checksums are
  always `None`. The v2 contract and the Reference bytes are the historical ones.
- The X1 Oracle observes the frozen audit API and the actual `ledger.post` binding. It verifies
  the injected notification sink through a keyword-only callable. The equivalent implementation
  exercises a real alias, forwarding wrapper, positional call to a same-name helper, and the
  injected sink. Mutations still detect direct positional API or sink calls and settlement
  bypassing the ledger.

Each cell binds its revision, task digest, materialized seed digest and Oracle digest. A2 also binds
the revised assignments and DAG digest. A0 and A2 pass the resolved task to the native and static
benchmark entry points. A1 and A3 use the same resolution for the materialized repository, public
tests and independent Oracle. The production admission report records the seed digest measured
from the materialized fixture for every cell.

The freeze uses three serial batches of 32 cells and the registered DeepSeek `deepseek-flash`
pricing snapshot. Each cell has a 1,000,000-token budget, 40 Main turns and 20 Worker turns.
The matrix has a 96,000,000-token nominal ceiling and per-cell, per-batch and cumulative cost
stops of USD 1, 10 and 20. The cell budget stop policy continues to require a verified pre-request
rejection and complete execution evidence before classifying the cell as a measured failure.

From the frozen clean checkout, `prepare` writes the manifest, plans, production admission and
unsigned authorization files to a fresh external directory. `launch` requires authority for all
three batches before the first claim, then re-checks settlement before each successive batch.

Reproduce the new offline task qualifications with
`python -m experiments.stage_f.revision_qualification --task f-p2-protocol-adapter --task f-x1-api-compat-audit --evidence-root PATH`.

Backlog for a separate study includes Main-driven context growth and historical Python assertion
counts. The formal run continues to use the strict Oracle verdict.
