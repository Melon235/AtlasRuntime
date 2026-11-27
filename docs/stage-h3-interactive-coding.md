# Stage H3 — Interactive Coding

The Workbench can run a coding goal against an isolated copy of a clean local Git working tree.
After a successful export, review the unified diff and explicitly apply or reject it. Review state
is independent of run status. The same operation and confirmation checks back the Web and CLI
entry points. Historical Replay remains read-only.

## Run the Workbench

Start the existing [local OpenSandbox service](stages/opensandbox-local.md). Configure a directory
whose immediate children are authorized task repositories. Store application run artifacts outside
the task repositories, historical evidence and authorization directories. Set `--runs-root` or
`ATLAS_APP_RUNS_ROOT` explicitly for interactive runs. The legacy CLI default is reserved for the
earlier modes.

```powershell
$env:ATLASOS_WORKSPACE_ROOTS = 'D:\Work\coding-workspaces'
$atlasRuns = 'D:\Work\atlas-product-runs'
.\.venv\Scripts\atlas.exe web --host 127.0.0.1 --provider deterministic --runs-root $atlasRuns
```

In the local Workbench, select a workspace, enter the goal, choose Direct or Dynamic and set the
budget. Select **Review changes for a clean Git workspace**. Authorize each new file by its exact
relative path. The deterministic Direct example modifies `solution/answer.txt` if present, or writes
the first explicitly authorized new path. The Dynamic script needs grants for
`solution/alpha.txt`, `solution/beta.txt` and `solution/integration.txt` if those files are new.
Both examples execute the real runtime using scripted responses and make zero model requests.

```powershell
$atlasRun = .\.venv\Scripts\atlas.exe run 'Update the sample deliverable' --mode main_direct `
  --interactive --workspace demo --provider deterministic --runs-root $atlasRuns --json |
  ConvertFrom-Json
.\.venv\Scripts\atlas.exe diff $atlasRun.run_id --runs-root $atlasRuns
.\.venv\Scripts\atlas.exe apply $atlasRun.run_id --runs-root $atlasRuns
# Or reject the reviewed changes:
# .\.venv\Scripts\atlas.exe reject-patch $atlasRun.run_id --runs-root $atlasRuns
```

For explicit noninteractive confirmation, pass `--yes --digest <digest-from-atlas-diff>` to the
decision command. Git changes are written to the original working tree only after confirmation.
The index and HEAD are left alone. A second identical decision returns the recorded result.

## How the boundary works

- The source snapshot identifies a canonical repository, its initial HEAD, and the hash of the
  included tracked files. The workspace must be clean at submission, materialization and Apply.
  The seed omits Git metadata, linked entries and known sensitive or protected paths. It is bounded
  to 20,000 files and 64 MiB. Named workspaces under the AtlasRuntime checkout and reserved
  experiment or authorization locations are refused.
- Existing eligible files and explicitly authorized new paths become exact grants in the existing
  ownership policy. The router and Worker policy still check delegated paths. OpenSandbox runs the
  goal on a private seed and exports the patch and workspace before teardown. Interactive provider
  failures preserve the original request evidence without retrying the request.
- Review uses the fixed run artifact and verifies its digest. Apply binds the run, workspace,
  snapshot and digest. The application rechecks its allowlist and source state under a
  repository lock, then uses `git apply --check` and `git apply`. It inspects the resulting Git
  status and verifies that the inverse patch is applicable before reporting `APPLIED`.
- A changed base or dirty workspace yields `CONFLICT` and retains the patch. An unverified apply
  yields `APPLY_FAILED` with available Git status evidence. AtlasRuntime never resets the user’s
  checkout. The lock coordinates AtlasRuntime clients. External editors and Git clients do not
  participate, so keep the original workspace idle while applying.

The first version supports regular UTF-8 text diffs with portable paths, up to 500 changed files and
2 MiB of reviewable patch data. The UI limits previews. Linked Git worktrees, binary changes, renames
as a single operation, symlinks, executable mode changes, dirty workspaces and automatic conflict
resolution are outside this scope. A crash during Apply requires inspection before another decision.
Cancellation continues to return `CANCEL_UNAVAILABLE`. Each follow-up goal is a new run. Product
acceptance remains `NOT_CONFIGURED` unless an independent contract is available.

## Verify offline

```powershell
$env:ATLASOS_RUN_LIVE_TESTS = '0'
$env:ATLASOS_RUN_LIVE_SANDBOX = '0'
$env:ATLASOS_TEST_H3_SANDBOX = '1'  # use the running local OpenSandbox service
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_h3_patch_review.py `
  tests/integration/test_h3_interactive_coding.py
```

The OpenSandbox integration tests drive the real application gateway with a deterministic adapter.
They cover Direct add/modify/delete, Dynamic routing and handoffs, reject, conflict, repeated Apply,
failed Provider usage and replay. [Browser evidence](evidence/stage-h3/) uses disposable Git fixtures.
