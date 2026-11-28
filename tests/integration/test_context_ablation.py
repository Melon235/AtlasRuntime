"""Exercise context interventions through the real Main-driven runner, offline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from anycode.types import StreamEvent, TokenUsage, ToolResultBlock, ToolUseBlock
from experiments.stage_b_plus import executor
from experiments.stage_f import context_ablation, proof, runners, scripted
from experiments.stage_f import release_candidate as release

from atlasos.app.main_driven import GoalContextExperiment
from atlasos.history_compaction import (
    COMPACTION_ROLLING_2,
    HISTORY_READ_TOOL,
    HistoryCompactionRuntime,
    HistoryReadInput,
    HistoryStore,
)
from atlasos.ledger import SQLiteActionLedgerStore
from atlasos.tools.registry import provider_tool_name


def test_context_candidate_binds_three_treatments_and_two_revisions():
    plan = release.batch_plan(context_ablation.BATCH_IDS[1])
    seen = set()
    for cell in plan["cells"]:
        release.bound_task(cell)
        options = context_ablation.options_for_cell(cell)
        assert vars(options) == context_ablation.TREATMENTS[cell["context_treatment"]]
        seen.add((cell["task_id"], cell["repetition"], cell["context_treatment"]))
        with pytest.raises(ValueError, match="frozen context intervention"):
            release.bound_task({**cell, "context_intervention": {"offloading": "off"}})
    assert len(seen) == len(plan["cells"]) == 12
    assert len({cell["cell_id"] for cell in plan["cells"]}) == 12
    assert plan["nominal_token_capacity"] == 12_000_000
    assert GoalContextExperiment(compaction=COMPACTION_ROLLING_2).offloading == "off"


def test_experimental_archive_validation_detects_modified_payload(tmp_path):
    store = HistoryStore(tmp_path, run_id="integrity-proof")
    ref = store.put(task_id="main", tool_use_id="read", content="original")
    assert store.invalid_written_refs() == []
    (tmp_path / f"{ref.ref_id}.txt").write_text("modified", encoding="utf-8")
    assert store.invalid_written_refs() == [ref.ref_id]


class TraceAdapter(scripted.StageFScriptedAdapter):
    def __init__(self, script, scenario, treatment):
        super().__init__(script, name=f"context-{scenario}")
        self.scenario = scenario
        self.treatment = treatment
        self.rows = []
        self.baseline_identity = {}

    async def stream(self, messages, options):
        try:
            reply = await self.chat(messages, options)
        except RuntimeError as error:
            yield StreamEvent(type="error", data=error)
            return
        for block in reply.content:
            if isinstance(block, ToolUseBlock):
                yield StreamEvent(type="tool_use", data=block)
        yield StreamEvent(type="done", data=reply)

    async def chat(self, messages, options):
        index = len(self.rows)
        blocks = [block for message in messages for block in message.content]
        results = [block for block in blocks if isinstance(block, ToolResultBlock)]
        tool_ids = {block.id for block in blocks if isinstance(block, ToolUseBlock)}
        system = str(options.system_prompt)
        first = messages[0].model_dump_json()
        assert all(result.tool_use_id in tool_ids for result in results)
        identity = {"system": system, "task": first}
        if not self.baseline_identity:
            self.baseline_identity.update(identity)
        assert identity == self.baseline_identity
        body = json.dumps([message.model_dump(mode="json") for message in messages])
        self.rows.append(
            {
                "messages_json_bytes": len(body.encode("utf-8")),
                "messages_json_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
                "task_sha256": hashlib.sha256(first.encode("utf-8")).hexdigest(),
                "results": [result.model_dump(mode="json") for result in results],
            }
        )
        if self.scenario == "provider_failure" and index == 3:
            raise RuntimeError("deterministic provider fault")
        reply = await super().chat(messages, options)
        if index == 4 and self.treatment != "C0":
            ref = next(
                match.group()
                for result in results
                if (match := re.search(r"hc-[0-9a-f]{24}", result.content))
            )
            tool = reply.content[0]
            assert isinstance(tool, ToolUseBlock)
            tool.input["ref_id"] = ref
        if self.scenario == "unknown_usage" and index == 3:
            return reply.model_copy(update={"usage": None})
        return reply.model_copy(
            update={
                "usage": TokenUsage(
                    input_tokens=scripted.estimate_tokens(body),
                    output_tokens=reply.usage.output_tokens,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=0,
                )
            }
        )


@pytest.mark.skipif(
    os.getenv("ATLAS_ADAPTER_REAL_SANDBOX") != "1", reason="requires local OpenSandbox"
)
async def test_context_ablation_real_main_driven(tmp_path):
    plan = release.batch_plan(context_ablation.BATCH_IDS[1])
    comparisons = {}
    original_results = {}
    missing_id = "hc-" + "0" * 24
    read_history = provider_tool_name(HISTORY_READ_TOOL)
    page_limit = 350
    for number, scenario in enumerate(("C0", "C1", "C2", "provider_failure", "unknown_usage"), 1):
        treatment = scenario if scenario in context_ablation.TREATMENTS else "C2"
        cell = next(
            cell
            for cell in plan["cells"]
            if cell["task_id"] == context_ablation.TASKS[0]
            and cell["repetition"] == 1
            and cell["context_treatment"] == treatment
        )
        task = executor.task_for_cell(cell)
        cell = {**cell, "order": number}  # Keep each offline provider registry identity distinct.
        # Repeat identical real tool calls. A complete result and a bounded read exercise
        # the ingress threshold and the smaller rolling-compaction threshold respectively.
        path = "docs/INTERFACE.md"
        steps = [
            ("file_read", {"path": path}),
            ("file_read", {"path": path, "limit": 27}),
            ("list_files", {"path": "."}),
            ("list_files", {"path": "."}),
            (read_history, {"ref_id": missing_id, "limit": page_limit}),
            (read_history, {"ref_id": missing_id, "limit": page_limit}),
            *proof._writes(task, tuple(sorted(task.reference))),
            ("run_public_tests", {"reason": "Verify the implemented changes"}),
        ]
        script = scripted.ScriptedRun(scripts={"main": steps}, default_marker="main")
        adapter = TraceAdapter(script, scenario, treatment)
        rows = adapter.rows

        root = tmp_path / scenario
        root.mkdir()
        run = runners.main_driven_cell_runner(provider_factory=lambda adapter=adapter: adapter)
        result = await run(cell, {"evidence_root": root, "runs_root": root})
        audit = runners.audit_stage_f_cell(
            cell=cell,
            plan=plan,
            run=result,
            prices=release.PRICING,
            cost_before=0,
        )
        (root / "trace.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        (root / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
        assert result.run_dir is not None
        assert result.oracle_sandbox is not None
        pristine = json.loads((result.run_dir / "session.json").read_text("utf-8"))
        requests = pristine["model_requests"]["records"]
        assert pristine["sandbox"]["created"] and pristine["sandbox"]["destroyed"]
        assert result.oracle_sandbox["sandbox_id"]
        assert result.oracle_sandbox["cleanup_failure"] is None
        assert not await asyncio.to_thread(Path(result.oracle_sandbox["root"]).exists)
        if scenario == "provider_failure":
            assert executor.STOP_PROVIDER_FAILURE in audit["stops"], audit
            assert len(requests) == 4 and requests[-1]["status"] == "FAILED"
            continue
        if scenario == "unknown_usage":
            assert executor.STOP_UNKNOWN_USAGE in audit["stops"], audit
            assert requests[3]["status"] == "COMPLETED" and requests[3]["usage"] is None
            continue
        assert audit["verdict"] == "PASS", (audit["stops"], result.error)
        assert result.session is not None
        stats = result.session["context_experiment"]
        assert bool(stats["ingress"]["offloads"]) is (scenario != "C0")
        assert bool(stats["history"]["results_archived"]) is (scenario == "C2")
        assert stats["history"]["read_success"] == (scenario != "C0")
        assert stats["history"]["read_failures"] >= 1
        assert stats["mapped_context_coverage_complete"]
        assert stats["recorded_file_reads"] == 2 and stats["repeated_recorded_file_reads"] == 1
        assert len(pristine["main"]["tool_calls"]) == len(steps)
        assert len({row["context_input"]["stable_prefix_sha256"] for row in requests}) == 1
        ledger = await asyncio.to_thread(SQLiteActionLedgerStore(result.run_dir / "ledger.db").list)
        assert len(ledger) == len(task.reference)
        assert all(
            row.state == "COMPLETED"
            and row.completion_kind == "EXECUTED_SUCCESS"
            and row.tool_name == "file_write"
            and row.result_digest
            for row in ledger
        )
        store = HistoryCompactionRuntime.create(
            run_dir=result.run_dir,
            run_id=pristine["run_id"],
        )
        task_id = pristine["main"]["task_id"]
        for sample in stats["ingress"]["samples"] + stats["history"]["samples"]:
            ref, content = store.store.load(sample["ref_id"], task_id=task_id)
            assert hashlib.sha256(content.encode("utf-8")).hexdigest() == ref.sha256
            assert content in original_results.values()
            for attributes in (
                {"run_id": "other-run", "task_id": task_id},
                {"run_id": pristine["run_id"], "task_id": "other-agent"},
            ):
                context = SimpleNamespace(execution_context=SimpleNamespace(attributes=attributes))
                denied = await store.reader.read(HistoryReadInput(ref_id=ref.ref_id), context)
                assert denied.is_error
        if scenario != "C0":
            page = json.loads(rows[5]["results"][-1]["content"])
            ref, original = store.store.load(page["ref_id"], task_id=task_id)
            assert page["content"] == original[:page_limit]
            assert page["sha256"] == ref.sha256
            assert page["next_offset"] == page_limit
        if scenario == "C0":
            original_results.update(
                {index: result["content"] for index, result in enumerate(rows[-1]["results"])}
            )
        comparisons[scenario] = {
            "stats": stats,
            "requests": len(rows),
            "input_bytes": sum(row["messages_json_bytes"] for row in rows),
            "peak_input_bytes": max(row["messages_json_bytes"] for row in rows),
            "peak_mapped_context_bytes": stats["mapped_context_peak_bytes"],
            "mapped_context_bytes": stats["mapped_context_cumulative_bytes"],
            "stable_prefix": requests[0]["context_input"]["stable_prefix_sha256"],
            "patch_digest": pristine["sandbox"]["patch_digest"],
            "system_sha256": rows[0]["system_sha256"],
            "task_sha256": rows[0]["task_sha256"],
        }
    (tmp_path / "comparison.json").write_text(json.dumps(comparisons, indent=2), encoding="utf-8")
    for field in ("requests", "system_sha256", "task_sha256", "stable_prefix", "patch_digest"):
        assert len({row[field] for row in comparisons.values()}) == 1
    assert comparisons["C2"]["input_bytes"] < comparisons["C1"]["input_bytes"]
    assert comparisons["C1"]["input_bytes"] < comparisons["C0"]["input_bytes"]
    assert comparisons["C2"]["mapped_context_bytes"] < comparisons["C1"]["mapped_context_bytes"]
    assert comparisons["C1"]["mapped_context_bytes"] < comparisons["C0"]["mapped_context_bytes"]
