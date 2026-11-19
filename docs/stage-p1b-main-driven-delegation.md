# Stage P1b — Real Main-driven Multi-Agent Delegation Validation

Branch `exp/stage-p1b-real-delegation`, based on `feat/stage-p01-execution-semantics` at
`5fad913f407707e7a3b975bce5abde69b377481d` (stage P1, `STAGE_P1_CORE_PASS_NO_DELEGATION`).
AnyCode 0.9.0. No runtime source file was modified in this stage.

**One live run, executed once. Decision: `STAGE_P1B_FAILED`.**

The Main ran nine turns against real DeepSeek inside a real OpenSandbox, read every file of both
plugin packages plus the public contract, and then **terminated on `side_effect_unknown`** when an
executed `bash` command exited non-zero. It made no edit, ran no public suite, and never called
`delegate_tasks`. The oracle failed because the deliverable was untouched. No second attempt was
made.

The failure is at the **Main execution loop** layer. It is not a Main proposal, Router admission,
worker execution, handoff, integration or verification failure — none of those layers was reached.

---

## A. Git provenance

| Item | Value |
|---|---|
| Repository | `D:\Work\AtlasRuntime`, remote `Melon235/AtlasRuntime` |
| Baseline verified | `feat/stage-p01-execution-semantics` at `5fad913f407707e7a3b975bce5abde69b377481d`, clean tree, `rev-list --left-right --count HEAD...origin/…` = `0 0` |
| Experiment branch | `exp/stage-p1b-real-delegation` (created from the baseline above) |
| Freeze commit | `ec36744` "feat(stage-p1b): freeze the dual-plugin fixture, the oracle and the live runner" |
| Working tree at run time | one uncommitted file: `run_stage_p1b_live.py` (recorded in `run_header.json` as `git_status`; the diff is 9 lines, a Windows read-only `rmtree` handler added while `--preflight` was being made repeatable — see §C.5). No frozen element is touched by it. |
| Fixture commit (inside the sandbox) | `4ddab3f509848dc8f3d80e18edd6f67b9851d573`, 23 files, one commit |
| Run id | `goal-fab81e7a170e46ef831cb1394503244f` |
| Scratch directory | `%TEMP%\atlas-p1b-live-1` (outside the repository; `experiments/results` untouched) |
| Attempt marker | `%TEMP%\atlas-p1b-live-1\attempts\attempt-1.json`, copied to the evidence bundle |
| Evidence bundle | `experiments/stage_p1b_live/evidence/run-1/` |

Frozen identity, all recorded in `run_header.json`:

| Item | Value |
|---|---|
| Goal digest | `sha256:c24c72738eee9e4eef86f7bfe6a1d43651a5949a540c1b4e9ce37fad0a86b778` |
| Main system prompt digest | `sha256:7e6fd829e51e094917960d309014acbe7c6636c7cec1a6f62213ec4ce986552a` (`MAIN_SYSTEM_PROMPT`, stage P0.1, unchanged) |
| Public command digest | `ec17b2e8247b02e2d9d06dbbea763aa97fe9eb03ff74d49b8fed12cc111c0048` |
| Oracle command digest | `e1d882b1fe93bf6c9d17d3051b2cb915b36ea315876678495e5923fb4bce0e32` |
| Acceptance spec digest | `sha256:086713c74409a28b2e6765323d05ee046f762aeb6cf5707569f26254e414e734` |
| Routing policy | `m.1.0`, digest `c6c29faf5d1acd2f59e60ab99ff268df78c5bfec629655852aa0ffa7dd9efea5`, `max_parallel_workers=2` |
| Provider / model | `deepseek` / `deepseek-chat` |
| Sandbox | `opensandbox`, `atlas-uat-python:3.12`, `network=none`, 1 CPU / 1 GiB, 20 min, `require_real=true` |
| Delegation | enabled, `max_parallel_children=2`, `max_children_per_call=2`, `max_delegation_rounds=4`, `child_timeout=300 s`, `child_max_turns=6`, `result_preview_chars=900` |
| Main | `max_turns=20`, write scope `["core/"]`, `token_budget=300000` (admission-based) |

## B. Fixture design

`experiments/fixtures/stage_p1b_dual_plugin` is a 23-file Python project (`eventpipe`) with two
vendored channel packages and one shared integration layer.

| Package | Responsibility | Independence |
|---|---|---|
| `plugins/file_events/` (`schema`, `parser`, `converters`, `errors`) | JSON Lines channel: `ev-…` string ids, ISO-8601 timestamps **with an explicit offset**, raw level vocabulary `trace/debug/info/warn/err/fatal`, comma-separated tags, `dict[str, str]` attributes, `FileEventError.code` vocabulary | reads only its own subtree |
| `plugins/message_events/` (`schema`, `parser`, `converters`, `errors`) | Message channel: integer ids, epoch **milliseconds**, uppercase `USER_JOIN`-style types, priority `1..5`, label lists, payload values that are not strings, `MessageEventError.code` vocabulary | reads only its own subtree |
| `core/` (`contracts`, `errors`, `adapter`, `registry`) | The unified `NormalizedEvent`, the `Severity` vocabulary, the plugin registry, and the two channel normalizers | the only writable layer |

The two channels deliberately share **no** vocabulary: identity shape, time unit, level/priority
naming, tag syntax, attribute typing, error class and default rules all differ, and every rule lives
inside its own package (`LEVEL_TO_SEVERITY`/`severity_of`/`split_tags` on one side,
`PRIORITY_TO_SEVERITY`/`coerce_priority`/`normalize_labels`/`stringify_payload` on the other).

Why this is not a five-line change:

* Two independent investigation surfaces. To map either channel a caller must read four files and
  reuse its converters; guessing produces a different answer for `warn`, `fatal`, priority `1`,
  absent level/priority, `None` payload values, duplicate labels and offset conversion.
* Shared integration work that only the Main can do: `core/adapter.py` has two symmetric holes
  (`normalize_file_event`, `normalize_message_event`) and `core/registry.py` has a third
  (`PluginRegistry.dispatch`, which must normalize *and* order by `(occurred_at, event_id)`).
* A compatibility surface: the two plugin suites (`tests/test_file_plugin.py`,
  `tests/test_message_plugin.py`, 25 assertions) describe behaviour that must not change, and the
  unified contract suite (`tests/test_public_contract.py`, 10 assertions) fails today.
* It stays inside the difficulty envelope: 23 files, no network, no extra dependency, solvable in
  one sandbox session — the reference implementation is 204 lines, both files together.

The Main-facing goal describes the user need, the two channels as a project fact, the compatibility
requirement and the write scope. It contains no delegation instruction of any kind, and
`MAIN_SYSTEM_PROMPT` was neither edited nor supplemented.

## C. Offline readiness

Four artefacts, all reproducible without a model: `verify_offline.py` (`offline_report.json`),
`check_router_admission.py` (`router_admission_report.json`), `rehearse_sandbox.py`
(`sandbox_rehearsal.json`) and `fixture_manifest.json`.

### C.1 Solvability

| State | Public suite (`python3 -B -m pytest -q tests`) |
|---|---|
| Shipped fixture | **10 failed, 25 passed** — the two plugin suites pass, the unified contract suite fails |
| Shipped fixture + reference implementation | **35 passed** |

### C.2 Oracle discrimination

Six implementations, each run through the public suite and the oracle:

| Implementation | Public | Oracle | Caught by |
|---|---|---|---|
| unimplemented (shipped) | FAIL | FAIL | `NotImplementedError` |
| `trace` mapped to `info` | **PASS** | FAIL | `file severity trace` |
| priority `1` mapped to `info` | **PASS** | FAIL | `message severity 1` |
| file tags not split | FAIL | FAIL | `file tags split, lowered, deduped, sorted` |
| message id without `msg-` prefix | FAIL | FAIL | `message id prefixed`, `message dispatch order` |
| dispatch not ordered | FAIL | FAIL | `file dispatch order`, `sample default ts`, `sample offset` |

Two wrong implementations pass the public channel and are refused by the oracle, so the hidden check
adds discriminating power rather than restating the visible suite.

### C.3 Independent investigation structure

`check_router_admission.py` drives the real `DelegationService.plan_context()` → `DelegationRouter`
path with the run's limits. It builds two read-only `research_worker` candidates — one per channel,
each with its own background, neither depending on the other — and checks the other legal shapes:

| Candidate set | Route | Reason / gates |
|---|---|---|
| two channel investigations, background supplied | `delegate_parallel`, both admitted, wave width 2 | `DELEGATION_ADMITTED` |
| one investigation | `delegate_one` | `DELEGATION_ADMITTED` |
| same two, no background | `direct` | `CONTEXT_INSUFFICIENT` (GATE_5) |
| same two, one asks to write `core/adapter.py` | `direct` | `WRITE_SCOPE_NOT_GRANTED` |
| same two, one declares a dependency | `direct` | `DEPENDENCY_NOT_SUPPORTED` |

`research_worker` holds `tool_search, dispatch_action, file_read, grep, list_files` (no write tool,
`can_delegate=false`), so a child can read either package and cannot modify anything. Two children
share no writable file because neither has a writable file.

This proves the runtime *would* admit two concurrent read-only investigations. It is not evidence
that a live Main will propose them; §F is.

### C.4 Oracle isolation

| Check | Result |
|---|---|
| Hidden programme text present in any fixture file | none (23 files scanned) |
| Registered as a tool | no — `run_public_tests` is the only suite channel the Main has |
| Written into the workspace | no — the oracle is one host-held command string |
| Public vs oracle command | different (`python3 -B -m pytest -q tests` vs `python3 -m pytest -q && python3 -c '…'`) |
| Independence from the public suite | the oracle asserts a broader matrix (all six levels, all five priorities, three error codes, attribute rendering, determinism, plugin regression) than the public suite |

A real-container rehearsal (no model call) confirmed all of this end to end: public `FAIL` (10
failed, 25 passed) → reference applied → public `PASS` (35 passed) and the oracle printed
`HIDDEN_OK`; the same command through `run_check` (the harness channel) returned exit 0; the
exported workspace contained no oracle text; the sandbox was destroyed.

### C.5 Freeze

`fixture_manifest.json` records the 23-file digest set, the goal digest, the oracle digest, the
public command digest, the write scope and the limits. The runner refuses to start when any of them,
`MAIN_SYSTEM_PROMPT`, or `src/` differs. `--preflight` can be run repeatedly before `--run`: the
fixture is rebuilt from the manifest each time, which is why the runner needed a Windows read-only
`rmtree` handler (the one uncommitted change recorded in §A).

## D. Live preflight

`--preflight` returned **READY**, 0 blocking failures: 19 runtime checks
(`live_preflight.py`, unchanged) plus 8 stage checks.

```text
sandbox_configured / sandbox_provider_real / sandbox_isolation_required / sandbox_workspace_root
source_repo_clean / main_write_scope / main_write_scope_relative / public_tests_configured
public_tests_separate_from_oracle / acceptance_configured / acceptance_digest_declared
budget_configured / delegation_bounds / context_metrics / provider_deepseek / model_fixed
api_key_present / live_provider_factory / scratch_run_directory                    19/19 OK

fixture_manifest_frozen   OK   23 frozen files; changed: []
goal_frozen               OK   sha256:c24c7273…
main_prompt_unchanged     OK   sha256:7e6fd829…  (stage P0.1)
runtime_tree_unmodified   OK   src/ clean
oracle_not_in_fixture     OK   hidden marker found in: []
public_oracle_separate    OK
main_write_scope_frozen   OK   ["core/"]
single_live_attempt       OK   marker written just before run_goal
```

The report is in `evidence/run-1/preflight-stage.json`; `run_goal` re-runs the runtime half itself
and writes `preflight-run.json` inside the run directory.

## E. Real execution trace

All times UTC, from the trajectory transcript and the conversation store.

```text
05:57:52.528  run_goal starts (attempt marker written)
05:58:00.804  sandbox provisioned (real OpenSandbox, network probe PASS, host separation clean)
05:58:08.42   t1  bash  find . -type f -name "*.py" | head -50; echo "---"; ls -la     ALLOW  ok
              t1  list_files .                                                         ALLOW  ok
05:58:11.71   t2  bash  cd /app 2>/dev/null || cd "$(pwd)"; cat README.md; …            DENY   OWNERSHIP_UNCLASSIFIABLE_SHELL
              t2  bash  for f in core/*.py; do echo …; cat "$f"; done                   DENY   OWNERSHIP_UNCLASSIFIABLE_SHELL
05:58:12.39   t3  bash  cat README.md                                                   ALLOW  ok
05:58:12.41   t3  bash  cat core/__init__.py core/contracts.py core/errors.py …         ALLOW  ok
05:58:15.75   t4  file_read core/adapter.py, tests/test_public_contract.py              ALLOW  ok
05:58:16.58   t5  file_read plugins/file_events/converters.py, parser.py                ALLOW  ok
05:58:17.71   t6  file_read plugins/file_events/schema.py, __init__.py                  ALLOW  ok
05:58:18.73   t7  file_read plugins/message_events/converters.py, schema.py             ALLOW  ok
05:58:19.77   t8  file_read plugins/message_events/parser.py, __init__.py               ALLOW  ok
05:58:20.58   t9  bash  (180 bytes)                                                     ALLOW  executed
05:58:20.60   t9  bash  (89 bytes)                                                      ALLOW  executed, exit 2
05:58:22.86   terminal: stop_reason=side_effect_unknown
05:58:29.79   sandbox CLOSED, run_terminal
```

The Main's own words, in order:

> I'll start by exploring the workspace structure.
> The `cat` output has lost newlines in some places. Let me read files individually properly.
> Now let me read the plugin packages.

The last executed command produced stdout from `.gitignore`, an `echo "==="`, `git log --oneline`
(`4ddab3f fixture: dual event channel project without the unified normalization path`) and the
stderr line `ls: cannot access 'cored': No such file or directory`, exit code 2. Its verbatim text
is not persisted: the conversation snapshot stops at the previous turn and the trajectory keeps only
the input digest (`bad18870…` for the successful call, `bf92ab50…`/89 bytes for the failing one; the
ledger's `request_digest` is `193a764c4b38…`). See §K, DEV-4.

Two of the eighteen calls were refused **before execution** and both were survivable — the P0.1
semantics working in production:

| Call | Reason | Main's recovery |
|---|---|---|
| `cd /app 2>/dev/null \|\| cd "$(pwd)"; cat README.md; …` | `OWNERSHIP_UNCLASSIFIABLE_SHELL`: `$(` cannot be analysed statically | re-issued `cat README.md` |
| `for f in core/*.py; do …; done` | `OWNERSHIP_UNCLASSIFIABLE_SHELL`: `for/do/done` has no analyzable write set | `cat core/*.py`, then switched to `file_read` |

## F. Delegation analysis

| Layer | Observation |
|---|---|
| Tool surface | `delegate_tasks` and `delegate.read` were registered and authorized (`tool_surface` in `session.json`) |
| Main proposal | **none** — `delegate_tasks` was called 0 times; `call_sequence` contains only `bash`, `list_files`, `file_read` |
| Router admission | **not consulted** — 0 batches, so this is *not* a Router `direct` decision and must not be reported as one |
| Child execution | 0 children; `max_observed_child_concurrency` = 0 |
| Handoff | 0 refs, 0 characters; `delegate.read` calls 0 |
| Integration | no edit; the patch is empty |
| Verification | static PASS, dynamic FAIL (see §G) |

The Main chose to do both investigations itself, sequentially, in five turns of two parallel
`file_read` calls — covering `core/adapter.py`, the public contract suite and all eight plugin
modules. That is a consistent choice, not a refusal: it had read the material it needed for both
mappings and had not yet reached the integration step when the loop was terminated.

Whether a live Main will *propose* a delegation is therefore still unanswered. This run adds no
evidence either way, because the run ended before the point at which the work would have been
integrated.

## G. Context metrics

All values are provider-reported; `unavailable_fields` is empty.

```json
{
  "main_requests": 9,
  "main_context_tokens_by_request": [2834, 3450, 3634, 7523, 10470, 12127, 13510, 15573, 16758],
  "main_peak_context_tokens": 16758,
  "main_cumulative_input_tokens": 85879,
  "main_cache_hit_tokens": 71552,
  "main_cache_miss_tokens": 14327,
  "main_completion_tokens": 855,
  "child_total_prompt_tokens": 0,
  "child_completion_tokens": 0,
  "system_total_tokens": 86734,
  "handoff_payload_chars": 0,
  "delegation_rounds": 0,
  "max_observed_child_concurrency": 0
}
```

Accounting: `85879 = 14327 + 71552`; `system_total = 85879 + 855`. Child columns are zero because
no child existed. No context-reduction claim is made: this run has one arm and no direct baseline.

## H. Correctness and safety

| Item | Result |
|---|---|
| Main edits | **none** — `workspace.patch` is 0 bytes (`sha256:e3b0c442…`, the empty digest), `patch_changed_files` 0 |
| Write scope | never exercised: no write was attempted, no ownership denial for a path |
| Public suite | 0 calls — the Main never reached the verification step |
| Independent oracle | static **PASS** (4/4: both modules present and parse), dynamic **FAIL**, exit 1, 1 466.8 ms, 16 821 chars of pytest output, no `HIDDEN_OK` |
| Acceptance verdict | **FAIL** |
| Sandbox | `opensandbox`, `provider_is_real=true`, network probe `PASS`, host separation clean, `destroyed=true`, `retained=false`, no error |
| Export | 23 files, 102 400 bytes, identical to the provisioned fixture |
| Ownership | `enforce`, coverage `PARTIAL`, 18 checked / 16 allowed / **2 denied**, both `OWNERSHIP_UNCLASSIFIABLE_SHELL`, neither executed |
| Action safety | every call went through M3; `tests.public` was registered but never used |
| Budget | 9 requests, 0 admission rejections, peak reserved 23 352, remaining 213 266, `learned_ratio` 1.0841 |
| Residual children / containers | none — no child was created, and the sandbox was destroyed |

## I. Cost and latency

| Item | Value |
|---|---|
| Wall clock, run start → terminal | **37.264 s** |
| Sandbox provisioned → ready | ~7.3 s |
| Provider requests | 9 |
| Prompt tokens | 85 879 (14 327 miss + 71 552 cache hit) |
| Completion tokens | 855 |
| Total tokens | 86 734 |
| Cache-hit ratio (prompt) | 83.3 % |
| Budget used | 28.9 % of 300 000 |
| Monetary cost | **UNAVAILABLE** — no `pricing` block is configured for this run, so nothing is estimated here |

## J. Final decision

**`STAGE_P1B_FAILED`**

Per §I.4 of the stage brief: the live execution failed and the oracle failed. The failure layer is
**Main execution loop**: the run stopped at `side_effect_unknown` after an *executed* `bash` command
returned exit code 2, so the Main never reached integration or verification.

What it is not:

* not a preflight failure (§D was READY; this is not `BLOCKED`),
* not a Router refusal and not a delegation outcome (§F: the Router was never consulted),
* not a budget or turn exhaustion (9 of 20 turns, 28.9 % of the token budget),
* not a safety violation (both denials were pre-execution and non-fatal; no write occurred),
* not an isolation failure (real provider, network probe PASS, host separation clean, destroyed).

No second attempt was made and nothing was adjusted afterwards to chase a different outcome.

## K. Stage B readiness

What this stage establishes, on real infrastructure:

1. The dual-plugin fixture is a genuine two-investigation task, and it is solvable end to end —
   proven both on the host and inside a real container by the reference implementation.
2. Its hidden oracle discriminates: two wrong implementations pass the public suite and fail the
   oracle; four others fail both.
3. The runtime would admit two concurrent read-only investigations for this fixture
   (`delegate_parallel`, both admitted, policy `m.1.0`/`c6c29faf…`), and refuses the shapes it
   should refuse.
4. A live Main, on its own, investigated both channel packages itself rather than delegating — but
   it was interrupted before the integration point, so this is not a clean observation of a
   deliberate direct-vs-delegate decision.

What remains proven only offline: concurrent child execution, context isolation between children,
handoff projection, and Main resume after a handoff (stages M/P0/P0.1 suites). This run did not
exercise any of them.

What only a controlled benchmark can establish: whether delegation reduces cost or context, and
under which task shapes a capable Main chooses it. Nothing in this stage may be cited as a
cost-benefit result.

Blocking defects found, all pre-existing and deliberately **not** fixed (the runtime is frozen for
this stage):

| Id | Defect | Impact |
|---|---|---|
| DEV-2 | `OpenSandboxProvider._execution_text` joins log messages with `""`, so every sandbox command's stdout loses its newlines (`src/atlasos/sandbox/opensandbox_provider.py:389-391`) | test output, `cat` output and exported patches arrive as one collapsed line; the Main noticed it ("The `cat` output has lost newlines in some places") and worked around it with `file_read`; `workspace.patch` is not applyable |
| DEV-3 | An **executed** tool that returns `is_error` without `retry_safe=True` is classified `UNCERTAIN` by the M4 bridge (`ledger/bridge.py:234-238`) and becomes `side_effect_unknown`, which AnyCode treats as terminal | a single failing shell command ends the Main's loop: no repair, no second try. P0.1 fixed only the *pre-execution* denials; this is the executed-command counterpart and it is the direct cause of this run's failure |
| DEV-4 | The conversation snapshot does not persist the tool-use blocks of the turn in which the run terminates | the verbatim command that ended the run is unrecoverable from the artefacts (only its digest and its error output survive) |

A future stage that wants a live Main to survive ordinary shell errors must decide DEV-3 explicitly;
it is a semantic change to the goal path (what `bash` failure means), not a bug fix, and it was out
of scope here. Stage B should not be started on the assumption that a live coding run is robust to a
failed command until that decision is taken.

No Stage B work was started.

---

## Evidence index

```text
experiments/stage_p1b_live/
  p1b_contract.py             frozen goal, public suite, hidden oracle, acceptance contract
  fixture_manifest.json       23 file digests + goal/oracle/public digests + limits
  verify_offline.py           baseline, reference, six implementations, isolation   -> offline_report.json
  check_router_admission.py   router admission for this fixture's task shape        -> router_admission_report.json
  rehearse_sandbox.py         real container, no model call                         -> sandbox_rehearsal.json
  run_stage_p1b_live.py       --preflight | --run, one attempt marker, no retry
  inspect_run.py              joins trajectory + conversation into one evidence object
  reference/core/             reference implementation (HARNESS ONLY, never provisioned)
  evidence/run-1/             preflight, run header, session, summary, ledger export,
                              conversation, transcript, empty patch, attempt marker
```
