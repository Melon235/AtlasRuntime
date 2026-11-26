"""Reproduce the 2026-11-19 incident deterministically.

Run from the project root: ``python diagnostics/reproduce.py``. The script uses the current code, a
deterministic clock and temporary state files; it prints the trace that ``incident.log`` captured.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eventsys.pipeline import Pipeline  # noqa: E402 - after the path fix
from eventsys.retry import RetryPolicy  # noqa: E402 - after the path fix


def run_burst(root: Path) -> dict:
    handled: list[str] = []
    published: list[str] = []
    rejected: list[str] = []
    pipeline = Pipeline(
        lambda event: handled.append(event.id),
        capacity=3,
        store_path=root / "burst-store.json",
        journal_path=root / "burst-journal.jsonl",
    )
    for index in range(1, 6):
        try:
            published.append(pipeline.submit("order", {"index": index}).id)
            print(f"publish e-{index} ok")
        except Exception as error:  # noqa: BLE001 - the trace records the real failure
            rejected.append(f"e-{index}")
            print(f"publish e-{index} rejected {type(error).__name__}")
    counts = pipeline.run_once()
    for event_id in handled:
        print(f"handled {event_id}")
    missing = [event_id for event_id in published if event_id not in handled]
    print("missing " + (",".join(missing) if missing else "none"))
    print(f"counts {counts}")
    print(f"stats {pipeline.stats()}")
    print(f"rejected {len(rejected)}")
    return {"published": published, "handled": handled, "missing": missing}


def run_replay(root: Path) -> dict:
    seen: set[str] = set()
    handled: list[str] = []

    def handler(event) -> None:
        if event.id not in seen:
            seen.add(event.id)
            raise RuntimeError("downstream timeout")
        handled.append(event.id)

    pipeline = Pipeline(
        handler,
        capacity=5,
        store_path=root / "replay-store.json",
        journal_path=root / "replay-journal.jsonl",
        policy=RetryPolicy(max_attempts=3, base_delay=1.0, max_delay=4.0),
    )
    pipeline.submit("order", {"index": 2})
    counts = pipeline.run_once()
    print(f"replay handled {handled}")
    print(f"replay counts {counts}")
    print(f"replay acked {pipeline.broker.acked_ids()}")
    print(f"replay dead {pipeline.broker.dead_letter_ids()}")
    print(f"replay clock {pipeline.clock.now():.1f}")
    return {"handled": handled, "counts": counts}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="eventsys-recover-") as directory:
        root = Path(directory)
        print("== burst ==")
        run_burst(root)
        print("== replay ==")
        run_replay(root)


if __name__ == "__main__":
    main()
