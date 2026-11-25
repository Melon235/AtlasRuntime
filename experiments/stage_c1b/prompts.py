"""Stage C1-B Main prompt: the Stage B+ bytes with one section rewritten.

The rewrite is derived from what the C1 Main actually did, not from a preference. After the Host
public suite passed, it spent six more turns on shell commands the ownership policy refuses
(``python -c``, ``python -m pytest``, heredocs), then the shared budget refused its next request.
The section it was following said "use ``run_public_tests``" and "change approach when tool use
produces no useful progress", but never said that interpreter invocations *are* refused here or
that a passing public suite is where verification ends.

Only ``## TESTING AND RECOVERY`` changes. Identity, planning, delegation, context and delivery
sections are byte-identical to ``main-v2.1-rc1-bplus``.

This is a **combined** intervention with the worker completion contract (see
``experiments/stage_c1b/plan.py``): the run that follows cannot be read as a single-variable
ablation of either change.
"""

from __future__ import annotations

import hashlib

from experiments.stage_b_plus import contract

V22_VERSION = "main-v2.2-rc1-c1b"

SECTION_START = "## TESTING AND RECOVERY"
SECTION_END = "## FINAL DELIVERY"

_SECTION_START = SECTION_START
_SECTION_END = SECTION_END

_SECTION = (
    "## TESTING AND RECOVERY\n"
    "\n"
    "`run_public_tests` is the only authorized way to execute the public suite. Use it when it is "
    "available.\n"
    "\n"
    "The ownership policy inspects every shell command. Interpreter invocations — `python -c`, "
    "`python -m pytest`, heredocs, compile loops — are deterministically refused in this "
    "runtime. A refusal is not a transient error: do not retry it or an equivalent form, and do "
    "not spend turns probing what the tool contract already states.\n"
    "\n"
    "A failed public test is diagnostic feedback. Investigate and repair within the allowed "
    "execution budget.\n"
    "\n"
    "A passing public test is necessary, not sufficient. Review the boundary conditions the task "
    "names with the tools that are permitted — file reads, grep, diffs — and repair the concrete "
    "defects you find.\n"
    "\n"
    "Reserve enough turns for integration, verification and final delivery. When no further "
    "verification is possible with the permitted tools, report the limits honestly and finish.\n"
    "\n"
    "Do not modify tests or acceptance criteria to conceal implementation problems.\n"
    "\n"
    "The independent Oracle is Host-controlled and unavailable as an agent tool.\n"
    "\n"
    "Do not claim hidden test success without evidence.\n"
    "\n"
)


def build_text() -> str:
    """The B+ prompt with exactly the one section replaced.

    Both boundaries must occur exactly once, the same rule the B+ ablation uses: a prompt edit that
    silently moved a heading would otherwise produce a different experiment under the same name.
    """

    base = contract.prompt_text("p3-bplus")
    if base.count(_SECTION_START) != 1 or base.count(_SECTION_END) != 1:
        raise ValueError("the B+ prompt no longer has the expected section boundaries")
    start = base.index(_SECTION_START)
    end = base.index(_SECTION_END)
    if end < start:
        raise ValueError("the B+ prompt sections are in an unexpected order")
    return base[:start] + _SECTION + base[end:]


V22_TEXT = build_text()
V22_DIGEST = "sha256:" + hashlib.sha256(V22_TEXT.encode("utf-8")).hexdigest()


def changed_sections() -> dict[str, str]:
    """The before/after of the one section, for the report and for review."""

    base = contract.prompt_text("p3-bplus")
    start, end = base.index(_SECTION_START), base.index(_SECTION_END)
    return {
        "replaced": base[start:end],
        "replacement": _SECTION,
        "unchanged_prefix_chars": str(start),
        "unchanged_suffix_chars": str(len(base) - end),
    }


__all__ = [
    "V22_DIGEST",
    "V22_TEXT",
    "V22_VERSION",
    "build_text",
    "changed_sections",
]
