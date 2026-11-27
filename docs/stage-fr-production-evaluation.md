# Stage F-R production evaluation

`stage-fr-final-96-v2` has three sequential 32-cell batches. The existing Stage B+ dispatcher resolves the registered plans and figures out budget headroom before each claim. The old Stage F and the F-R v1 candidate retain their original identities and evidence.

## Prepare and review

From the clean execution checkout, use a fresh, short evidence root outside the repository. The path length matters for nested Windows fixtures.

```powershell
& ./.venv/Scripts/python.exe -m experiments.stage_f.release_candidate prepare --out D:/Work/fr2r
```

Preparation writes the manifest, tracked executor file digests, three plans, pending authorization records, default-runner admission and read-only settlement preflight. The production dispatcher re-derives the same manifest and plans from the actual checkout and compares them to the external frozen records. Admission checks credential presence, the official DeepSeek endpoint and OpenSandbox health. It materializes fixture files and performs no model request or claim.

The manifest binds the execution HEAD, the executor digest, task bytes, public tests, independent Oracles, pricing and settlement. Existing authority verification checks the batch-specific digest, exact cell order, approved HEAD, signed budget, evidence of Owner approval and claim state. Keep the three authorization files `<plan-id>.authorization.json` in the evidence root. Only the Owner supplies the separate approval and its provenance after reviewing the frozen records.

## Single formal entry

After the Owner approves all three batches, run the following from the frozen checkout:

```powershell
& ./.venv/Scripts/python.exe -m experiments.stage_f.release_candidate launch --out D:/Work/fr2r
```

Launch checks the three authorizations up front and rechecks each inside the production dispatcher. It runs batches in order and exits with status 2 when authorization is absent or a batch stops. Missing identity files and altered contracts also refuse execution. A measured Oracle failure can leave the matrix running when integrity and resource evidence are complete. An infrastructure, provider, unknown-usage, evidence, budget or claim stop ends the sequence. The next batch needs complete prior v2 reports and consumed prior claims under the same executor, pricing and settlement identity. Neither the CLI nor dispatcher retries a cell or reuses a consumed claim.

All four modes request `deepseek-flash`. A0/A2 bind the model into their benchmark runner settings and resolved AtlasConfig. A1/A3 bind it into benchmark GoalRunOptions and AtlasConfig, with Main and Workers using the same model. The provider journal saves the requested and returned identity. Failed or incomplete requests preserve observed usage separately from unknown totals.

## Budget and prices

All cells have 1,000,000 nominal system tokens, Main 40 turns, Worker 20 turns, at most two parallel children and one attempt. Matrix concurrency is one. Total nominal capacity is 96 million tokens. The frozen candidate ceilings are USD 1 per cell, USD 10 per batch and USD 20 across the three batches. Later batches receive the smaller of their own ceiling, the amount the Owner authorized and remaining global allowance.

Prices are USD 0.006, 0.30 and 1.20 per million Cache-hit input, Cache-miss input and output tokens respectively, using published peak rates checked on 2026-11-27. Missing cache splits are priced as misses. Burn estimates at H4 and historical A0 scales are approximately USD 0.94 and 8.55 for 96 cells assuming the H4 input/output mix and all Cache misses. Actual usage will vary by task and arm. Token admission and settlement are estimates, and an in-flight request can exceed a threshold before the dispatcher accounts for it. [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)
