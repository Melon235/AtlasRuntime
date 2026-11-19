"""Rendering a schedule as text.

The text form exists so a plan can be pasted into a build log and diffed between two runs. It is
derived from the plan only -- it never recomputes anything.

Format, LF line endings, no trailing whitespace:

```text
plan <status> makespan=<makespan>
<start:04d>-<end:04d> <task_id> <resources>
...
unschedulable <task_id>,<task_id>,...
```

* one line per scheduled task, in the order the plan already holds;
* ``<resources>`` is ``-`` when the task demands nothing, otherwise ``name=units`` pairs sorted by
  name and joined with ``,`` with no spaces;
* the ``unschedulable`` line appears only when the plan has unschedulable tasks, and lists their
  ids sorted, joined with ``,``;
* the text ends with a single ``\\n``.
"""

from __future__ import annotations

from workgrid.scheduler.contracts import SchedulePlan


def render_plan(plan: SchedulePlan) -> str:
    """Return the deterministic text form described in the module docstring."""

    raise NotImplementedError("render_plan is not implemented yet")
