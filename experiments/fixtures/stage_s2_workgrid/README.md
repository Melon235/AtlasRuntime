# WorkGrid

Deterministic offline scheduling for a batch of tasks that have dependencies and resource demands.

WorkGrid reads one plan document -- pools, availability windows, tasks -- and produces one
reproducible schedule. It is used by build tooling that must be able to re-derive the same plan on a
different machine and diff it against the previous one.

## Packages

| Package | Responsibility | State |
|---|---|---|
| `workgrid.dependencies` | Graph structure, reference and cycle validation, stable topological order | **complete** |
| `workgrid.resources` | Pools and capacity, availability windows, per-placement constraints | **complete** |
| `workgrid.scheduler` | The shared plan built from both, plus its text form | **incomplete** |

`workgrid.scheduler.contracts` is complete: it is the published shape of a plan (statuses, reason
codes, dataclasses, canonical JSON). `workgrid.scheduler.engine.build_schedule` and
`workgrid.scheduler.report.render_plan` are not implemented; their docstrings state the contract
they must satisfy.

The two rule packages are independent on purpose. Neither imports the other, and neither imports the
scheduler, so a caller can use them without ever building a plan.

## Reading a plan document

```json
{
  "pools": [{"name": "workers", "capacity": 2}],
  "windows": [{"start": 0, "end": 4}, {"start": 6, "end": 10}],
  "tasks": [
    {"id": "build", "duration": 2, "demand": {"workers": 1}},
    {"id": "test", "duration": 1, "depends_on": ["build"], "demand": {"workers": 1}}
  ]
}
```

* `pools` -- one capacity per resource, constant over time.
* `windows` -- half-open slot ranges that are open for work; anything between them is closed.
* `tasks` -- an id, a duration in slots, optional `depends_on`, optional `demand` per resource.

`samples/plan.json` is a small but complete example.

## Tests

```sh
python3 -m pytest -q tests
```

* `tests/test_dependencies.py` and `tests/test_resources.py` describe behaviour callers already rely
  on. They pass today and must keep passing.
* `tests/test_public_scheduler.py` is the acceptance suite for the scheduling layer. It fails until
  the engine and the renderer are implemented.

## Scope

Changes belong in `workgrid/scheduler/`. The dependency and resource packages are compatibility
surface: their public functions, error types and ordering guarantees are what other tools import.
