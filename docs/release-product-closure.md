# Release product closure

H4 remains `STAGE_H4_REAL_PRODUCT_DEMO_STOPPED`. The original run `app-3d4a385eb4fc` reached
`main_max_turns` after 12 completed requests. The tool payloads and trajectory tie the rejected
pytest commands on turns 5, 8, 9 and 12 to `OWNERSHIP_UNCLASSIFIABLE_SHELL`. The run also had a
path refusal and other tool calls. The evidence establishes the test-channel gap but cannot
establish that adding the channel would have let the same live run complete. The original patch
export passed independent tests. The run failed and its patch review is `UNAVAILABLE`.

## Authorize product tests

Set `ATLASOS_PRODUCT_TESTS` in the environment before starting the CLI or Workbench. It is a JSON
object mapping a canonical, absolute workspace path to one explicitly approved, portable relative
Python test file. The workspace still needs to be allowed through `ATLASOS_WORKSPACE_ROOTS`.
For interactive runs, the selected test file must be in the tracked snapshot.

```powershell
$env:ATLASOS_WORKSPACE_ROOTS = 'D:\Work\coding-workspaces'
$env:ATLASOS_PRODUCT_TESTS = '{"D:/Work/coding-workspaces/release-demo":"tests/test_example.py"}'
.\.venv\Scripts\atlas.exe web --host 127.0.0.1 --provider deterministic --runs-root D:\Work\atlas-product-runs
```

ApplicationService constructs a `PublicTestContract` for the selected workspace and passes it to
the existing goal runner. Main can call `run_public_tests` with an optional reason. The tool runs
the fixed pytest command in OpenSandbox, with a 120-second timeout, a four-call limit and bounded
output. The command selects the approved file explicitly, disables plugin autoload and conftest,
and supplies its own pytest configuration. Test and application code still execute inside the
sandbox. The Host should choose a test file that works without repository pytest configuration.

The Workbench and `atlas result <run-id>` show recorded test results and exit codes. Unconfigured
workspaces report `NOT_CONFIGURED`. The existing Shell ownership policy, benchmark test contracts,
independent Oracle and failed-run patch boundary remain in effect.

## Replay provider requests

Replay reads `RequestJournal.snapshot()` records from `provider_requests.json` or the session
fallback. Older list, `requests` and `entries` formats are supported. When the producer truncates
records, the page distinguishes the retained rows from the full-run counts. Missing agent identity
and usage remain unknown. Error details are redacted and bounded before display.

The original H4 journal projects as `AVAILABLE`, with 12 retained requests, 12 `COMPLETED`, zero
`FAILED` requests, and `deepseek-chat` → `deepseek-flash`. The run is still `FAILED`, effective
execution is `false`, and its patch review is still `UNAVAILABLE`.
