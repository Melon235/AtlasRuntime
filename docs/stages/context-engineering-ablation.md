# Direct-only Context Engineering ablation

`stage-fr-context-ablation-v1` uses one authorization and one claim for a 12-cell candidate.
It uses the production Main-driven Direct runner, the C1 and X2 revision 2 fixtures and
independent Oracles, DeepSeek `deepseek-flash`, and OpenSandbox. Each cell has 40 Main
turns and a shared nominal budget of 1,000,000 tokens. Workers are disabled. The cells
run serially with one attempt each.

| Treatment | Tool result ingress | Conversation compaction |
|---|---|---|
| C0 | Off | Off |
| C1 | Archive results exceeding 4,000 characters | Off |
| C2 | Archive results exceeding 4,000 characters | Keep two recent tool rounds and archive older results exceeding 800 characters |

All treatments use the same prompt and native tool definitions. The registry exposes
`history_read` while the action catalog uses the logical name `history.read`. The
reader pages through the calling task's archived results from the same run. The
archive records SHA-256 and length, preserves the full original, and shares a store
between the two transforms. The rolling hook retains system and task instructions,
assistant tool calls, and result pairing. It runs before conversation persistence
and budget estimation. Ingress runs after the actual tool outcome is recorded.
Neither transform issues inference requests. The experimental options are host
supplied and require a sandboxed Direct-only benchmark.

## Evidence and comparisons

Use the actual `provider_requests.json` for per-request input, output and cache
usage. The `provider_usage` field preserves missing cache fields and records adapter
latency. The `context_input` field records canonical JSON bytes and digest of the
mapped messages and tool definitions before HTTP encoding, plus a stable-prefix
digest. The two measures have different units. SDK and transport encoding bytes
are outside the measurement. Use `evidence.json` for peak and cumulative input,
system tokens, archive counts and bytes, reference reads and serialized page bytes,
local transform time, Main turns, tool counts, repeat path reads, Oracle outcome
and the budget ledger. Use `matrix-report.json` for estimated cost and end-to-end
latency. The reference overhead includes observable page bytes and the usage of
subsequent Main requests. Those requests include the rest of the context too.

Pair by task and repetition. Compare C0 to C1, C1 to C2, and C0 to C2. Summarize all
valid pairs and separately the pairs where both cells delivered successfully.
Report changes in correctness alongside size and cost. Verified per-cell admission
exhaustion is a measured failure. Missing or inconsistent evidence, unknown usage,
resource violations, provider failures, unsafe side effects or sandbox
failures stop the batch. Report whether each mechanism triggered before drawing
mechanism conclusions. This is a small engineering comparison with three
treatments. It cannot estimate a factorial interaction or establish statistical
significance across tasks.

The offline proof uses a deterministic provider and the production runner with
real OpenSandbox and the independent Oracle. Its token values are estimates from
the scripted input. Local model calls and HTTP model calls during this proof are
zero. The proof establishes behavior for its fixed trace.

## Preparation and authorization

Run offline preparation from the clean experimental checkout with the usual
OpenSandbox and production preflight configuration. Use a fresh external output
directory, for example `D:\Work\ctx12`.

```powershell
.venv/Scripts/python.exe -m experiments.stage_f.release_candidate prepare --experiment stage-fr-context-ablation-v1 --out D:/Work/ctx12
```

The candidate settles against 12,000,000 nominal tokens, USD 5 estimated total
and USD 1 estimated per cell. At frozen peak rates, a scenario with 850,000 input
tokens, 40,000 output tokens, and a 90% input cache hit ratio per cell estimates
USD 0.93708 across 12 cells. With the same input and output volumes and all
cache misses, the estimate is USD 3.636. Unknown cache split is priced as a
miss for settlement. These thresholds are estimated stop conditions, with
per-cell reconciliation after use.

After separate Owner authorization for the exact clean checkout, manifest and
plan, the registered launch entry is:

```powershell
.venv/Scripts/python.exe -m experiments.stage_f.release_candidate launch --experiment stage-fr-context-ablation-v1 --out D:/Work/ctx12
```

Preparation writes an unsigned authorization candidate. Historical runs are
excluded from the 12 cells.
