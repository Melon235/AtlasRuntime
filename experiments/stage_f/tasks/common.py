"""Shared construction helpers for Stage F tasks.

The dataclass is the frozen Closure-2 one: a Stage F task is the same object with the same digest
functions, so ``prepare_fixture``, ``public_test_spec``, ``acceptance_spec`` and the sandboxed oracle
all keep working without a second task model.
"""

from __future__ import annotations

from experiments.evaluation.closure2.catalog import SHARED_HEADER, Task

__all__ = ["SHARED_HEADER", "Task", "shared_block"]


def shared_block(requirement: str, interface: str, constraints: str, acceptance: str) -> str:
    """The block every worker of a sample receives, in the frozen Closure-2 shape."""

    return "\n".join(
        [
            SHARED_HEADER,
            "",
            "PUBLIC REQUIREMENT",
            requirement,
            "",
            "PUBLIC INTERFACE CONTRACT",
            interface,
            "",
            "DATA AND COMPATIBILITY CONSTRAINTS",
            constraints,
            "",
            "PUBLIC ACCEPTANCE",
            acceptance,
            "",
            "WORKSPACE RULES",
            "- The project is at `/workspace/project`. Read any project file you need.",
            "- Do not search hidden tests, scoring output, evaluation artifacts or Git history for a "
            "reference implementation. They are not part of this task.",
            "- Report the files you changed, the commands you ran and their real output.",
        ]
    )
