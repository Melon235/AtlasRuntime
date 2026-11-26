"""Run report rendering (v2)."""

from __future__ import annotations

from ingest.model import RunResult


def render(result: RunResult) -> str:
    """Render the run summary exactly as the contract orders it."""

    lines = [
        f"source={result.source}",
        f"records={result.record_count}",
        f"batches={result.batch_count}",
        f"cursor={result.cursor}",
        f"skipped={result.skipped}",
    ]
    for name in sorted(result.totals):
        lines.append(f"total.{name}={result.totals[name]}")
    return "\n".join(lines)
