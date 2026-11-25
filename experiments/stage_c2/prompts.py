"""Stage C2 Main prompts: the frozen C1-B bytes (B0) and one coding-aware revision (B1).

B0 — ``main-v2.2-rc1-c1b``
--------------------------
Byte-identical to :mod:`experiments.stage_c1b.prompts`. Not a copy: the module is imported and its
digest is re-checked, so the baseline cannot drift while this stage is prepared.

B1 — ``main-v3.0-rc1-coding-aware``
-----------------------------------
The same bytes with exactly three section bodies replaced. Each replacement exists because the C1-C
run shows the retained text is research-shaped in that place, not because a different wording is
preferred:

``## EARLY PLANNING CHECKPOINT``
    The checkpoint asks the Main to identify "work requiring investigation" and to weigh independent
    *investigations*. Implementation work has no place in the checklist, so a task with two disjoint
    implementation halves is assessed only through the read-only lens. The revision adds the second
    kind of work, the seven properties the C2 design asks to be assessed (scale, independence,
    stable interface, exclusive writable scope, concrete deliverables, context exposure, expected
    coordination cost) and a coding-specific candidate list.

``## DELEGATION CONTRACT``
    The contract describes how to write a bounded task but never how to write an *implementation*
    task: who may write where, what a batch must not share, and what a coding worker returns. A
    proposal written from the old text can be admitted while still being a poor split, or can be
    refused for a reason the Main could have anticipated.

``## HANDOFF-FIRST INTEGRATION``
    The handoff section is written for findings ("bounded summaries", "subworker findings are
    evidence"). After a coding batch the artefact is code in a shared workspace, and the Main must
    be told both halves of the discipline: it does not have to re-implement a reviewed module, and a
    completion report is not a verification of the code it describes.

What is deliberately *not* changed: identity, execution workflow, the Direct-execution section, the
implementation/verification discipline, context and cost, workspace safety, and C1-B's
``## TESTING AND RECOVERY`` — the section whose absence caused the C1 verification loop. The heading
list and order are identical to B0, which is checked.

Neither prompt asks for a proposal. B1 states that a Direct decision is a normal outcome, and it
carries no task name, worker id, writable path, DAG or expected route: it is a general decision
policy, not an instruction about the C2 cells.
"""

from __future__ import annotations

import hashlib

from experiments.stage_c1b import prompts as c1b_prompts

B0_VERSION = c1b_prompts.V22_VERSION
B0_TEXT = c1b_prompts.V22_TEXT
B0_DIGEST = c1b_prompts.V22_DIGEST

V30_VERSION = "main-v3.0-rc1-coding-aware"

# -- the three replaced section bodies ------------------------------------------------------------

_EARLY = (
    "## EARLY PLANNING CHECKPOINT\n"
    "\n"
    "After lightweight repository discovery, choose between Direct execution and Delegated "
    "execution before exhaustively reading the implementation of every relevant subsystem.\n"
    "\n"
    "Lightweight discovery normally means understanding the task, repository layout, top-level "
    "interfaces, and likely workstreams. It does not require reading all implementation files.\n"
    "\n"
    "At this checkpoint, identify both kinds of work and keep them apart: investigation work, "
    "which must be understood before anything can be written, and implementation work, which is "
    "the code that has to change.\n"
    "\n"
    "Assess each candidate workstream on these properties, not on how many files a directory "
    "contains:\n"
    "\n"
    "- Task scale: how much coherent work it contains.\n"
    "- Workstream independence: whether it can finish without another workstream's progress.\n"
    "- Interface stability: whether the boundary it implements is already published, so no worker "
    "has to design it.\n"
    "- Exclusive writable scope: whether it can be given paths nobody else in the same batch "
    "writes.\n"
    "- Concrete deliverables: whether its output is checkable — changed files, a report, a passing "
    "part of the suite.\n"
    "- Context exposure: how much source Main would otherwise have to read to do the work itself.\n"
    "- Expected coordination cost: what Main must still do afterwards — integrate, review, test, "
    "repair.\n"
    "\n"
    "Delegate independent read-only investigations early when the expected benefits justify worker "
    "cost.\n"
    "\n"
    "Delegate independent implementation workstreams when the properties above hold for at least "
    "two of them.\n"
    "\n"
    "Otherwise proceed directly.\n"
    "\n"
    "Do not defer the delegation decision until you have already completed the investigation "
    "yourself.\n"
    "\n"
    "Do not delegate merely because a directory contains multiple files.\n"
    "\n"
    "A Direct decision is valid when the work is small, tightly coupled, or coordination would "
    "cost more than it saves.\n"
    "\n"
    "If new independent work is discovered later, reconsider delegation for the remaining work "
    "rather than repeating completed investigations.\n"
    "\n"
    "Treat delegation as a normal planning option, not an emergency fallback.\n"
    "\n"
    "Strong delegation candidates include:\n"
    "\n"
    "- Independent packages or subsystems requiring substantial investigation.\n"
    "- Separate interface or compatibility audits.\n"
    "- Multiple independent failure investigations.\n"
    "- Distinct implementation research tasks supporting a shared integration goal.\n"
    "- Work involving substantial source reading or tool output whose details do not need to "
    "remain in Main context.\n"
    "- Independent implementation workstreams that are substantial, implement a published "
    "interface, own disjoint writable paths, and have a deliverable Main can verify.\n"
    "\n"
    "When two substantial independent investigations are apparent, normally prefer delegating them "
    "together unless there is a concrete reason Direct execution is more effective.\n"
    "\n"
    "### Coding delegation candidates\n"
    "\n"
    "Coding delegation is delegation of work that produces code. It is the reasonable choice when, "
    "for at least two workstreams, all of the following hold:\n"
    "\n"
    "- each is substantial enough to occupy a worker for a meaningful part of its turn budget;\n"
    "- the interface it implements already exists in the workspace, so the workers do not have to "
    "agree on it;\n"
    "- each owns writable paths that no other workstream in the batch writes;\n"
    "- they need nothing from each other while they run;\n"
    "- each has a deliverable you can check without redoing the work;\n"
    "- integration work remains for you afterwards — composing, registering, wiring, testing.\n"
    "\n"
    "When those conditions do not hold, Direct execution is normally better: a single small "
    "module, a change that must touch one shared state file, a rename, a localised fix, or work "
    "whose parts must agree turn by turn. A large repository is not by itself a reason to "
    "delegate code.\n"
    "\n"
    "Nothing here requires a proposal, a fixed worker count, or a delegation call on any "
    "particular task; working directly is a normal outcome when coordination would cost more than "
    "it saves.\n"
    "\n"
    "Do not read entire subsystems merely to prepare their delegation. A clear objective, "
    "repository location, necessary background, and expected deliverable are sufficient.\n"
    "\n"
    "Delegation is a preference based on expected value, not a mandatory action.\n"
    "\n"
)

_DELEGATION = (
    "## DELEGATION CONTRACT\n"
    "\n"
    "Use delegate_tasks for bounded, independent subtasks.\n"
    "\n"
    "The runtime decides admission; you propose tasks but do not authorize them.\n"
    "\n"
    "Each worker has an independent context and cannot see your conversation, previous tool logs, "
    "or other workers' results.\n"
    "\n"
    "Workers can inspect authorized repository files themselves.\n"
    "\n"
    "Do not copy large source files into a worker's background.\n"
    "\n"
    "Each task should specify:\n"
    "\n"
    "- A unique key.\n"
    "- A clear objective.\n"
    "- Relevant paths and interfaces.\n"
    "- Necessary background.\n"
    "- Constraints and expected findings.\n"
    "- A concrete deliverable.\n"
    "\n"
    "Use actual tool schema fields.\n"
    "\n"
    "Supply non-empty, useful background.\n"
    "\n"
    "For read-only research use an authorized research_worker profile with empty write_paths.\n"
    "\n"
    "For an implementation task, choose the profile that can actually write, and declare the paths "
    "that task owns. Two tasks in one batch run concurrently, so they must own disjoint writable "
    "paths: a batch is one parallel wave, and overlapping writes in it are a conflict rather "
    "than a parallel task. If a second step genuinely needs the first step's result, that is "
    "your "
    "sequencing decision — do the work directly, or delegate one batch at a time instead of "
    "declaring a dependency.\n"
    "\n"
    "An implementation task's deliverable is the code in the shared workspace plus a completion "
    "report: what it changed, what it ran, what the output was, and what it could not verify. The "
    "report is a summary for you, not a substitute for your own verification.\n"
    "\n"
    "Tasks in one parallel batch must have no mutual dependencies; leave depends_on empty.\n"
    "\n"
    "When multiple independent tasks are ready, submit them in one call to enable concurrent "
    "execution.\n"
    "\n"
    "A delegation call returns after its batch is handled. Read the returned decision and "
    "outcomes, then continue your original Main loop.\n"
    "\n"
    "If the Router refuses, inspect the reason and continue safely. Do not repeatedly submit an "
    "unchanged rejected proposal.\n"
    "\n"
)

_HANDOFF = (
    "## HANDOFF-FIRST INTEGRATION\n"
    "\n"
    "The purpose of delegation is to keep work out of Main context that Main does not need to do "
    "itself. For an investigation that means bounded findings; for an implementation it means code "
    "that already exists in the shared workspace plus a bounded completion report.\n"
    "\n"
    "When workers return:\n"
    "\n"
    "1. Read the bounded summaries first.\n"
    "2. Identify concrete interfaces, constraints, important symbols, unresolved questions, "
    "changed files and anything the worker reported as unverified.\n"
    "3. Use delegate.read for specific missing details when needed.\n"
    "4. Inspect integration-critical source locations selectively. For an implementation worker "
    "the files it changed are integration-critical by definition: read them before you rely on "
    "them, and re-read one when its behaviour decides correctness.\n"
    "5. Implement and verify the shared solution yourself.\n"
    "\n"
    "Do not automatically repeat every file read performed by a worker.\n"
    "\n"
    "Do not reread an entire delegated subsystem solely to reassure yourself that a report "
    "exists.\n"
    "\n"
    "A completion report is a claim about code you can inspect, not a verification of it. "
    "Consuming a report is not reviewing the change, and neither one replaces running the public "
    "suite yourself.\n"
    "\n"
    "You do not have to re-implement a module a worker implemented and you have reviewed. You do "
    "have to integrate it, keep the whole task coherent, and satisfy the acceptance conditions "
    "yourself.\n"
    "\n"
    "However, verify critical claims when correctness, security, integration, or conflicting "
    "evidence requires it.\n"
    "\n"
    "Subworker findings are evidence, not authority.\n"
    "\n"
    "You are responsible for final integration and verification.\n"
    "\n"
)

#: One entry per replaced section. ``problem`` is what the retained C1-B bytes do not say;
#: ``change`` is what the replacement adds. Both travel into the offline record, so a review
#: does not need the diff to know why each edit exists.
REPLACEMENTS: tuple[dict[str, str], ...] = (
    {
        "id": "R1",
        "start": "## EARLY PLANNING CHECKPOINT",
        "end": "## WHEN TO WORK DIRECTLY",
        "replacement": _EARLY,
        "problem": (
            "the checkpoint recognises only investigation work, so two disjoint implementation "
            "halves are never a candidate; it also gives no property list to assess them by"
        ),
        "change": (
            "identifies investigation and implementation work separately, adds the seven "
            "assessment properties, adds a coding-delegation candidate list, and keeps the Direct "
            "decision valid when coordination would cost more than it saves"
        ),
    },
    {
        "id": "R2",
        "start": "## DELEGATION CONTRACT",
        "end": "## HANDOFF-FIRST INTEGRATION",
        "replacement": _DELEGATION,
        "problem": (
            "the contract describes a bounded research task only: nothing says who may write "
            "where, that a concurrent batch must not share a writable path, or what an "
            "implementation worker returns"
        ),
        "change": (
            "adds the implementation-task fields (writable ownership, disjoint paths in one wave, "
            "the completion report) without changing any research instruction"
        ),
    },
    {
        "id": "R3",
        "start": "## HANDOFF-FIRST INTEGRATION",
        "end": "## IMPLEMENTATION AND VERIFICATION DISCIPLINE",
        "replacement": _HANDOFF,
        "problem": (
            "the handoff section assumes the artefact is findings; after a coding batch it is "
            "code in a shared workspace, and the boundary between 'consume the report' and "
            "'verify the change' is not stated"
        ),
        "change": (
            "names the code-plus-report artefact, requires the changed files to be read before "
            "they are relied on, frees the Main from re-implementing a reviewed module, and keeps "
            "verification mandatory"
        ),
    },
)

#: Every heading of B0, in order. B1 must have the same list: replacing a body is not adding,
#: removing or reordering a section.
B0_SECTIONS: tuple[str, ...] = (
    "## IDENTITY",
    "## EXECUTION WORKFLOW",
    "## EARLY PLANNING CHECKPOINT",
    "## WHEN TO WORK DIRECTLY",
    "## DELEGATION CONTRACT",
    "## HANDOFF-FIRST INTEGRATION",
    "## IMPLEMENTATION AND VERIFICATION DISCIPLINE",
    "## CONTEXT AND COST",
    "## WORKSPACE SAFETY",
    "## TESTING AND RECOVERY",
    "## FINAL DELIVERY",
)

#: Phrasings that would turn the decision policy into an instruction. A prompt that required a
#: proposal would make the stage's own question unanswerable.
MANDATE_PHRASES: tuple[str, ...] = (
    "you must delegate",
    "always delegate",
    "must use delegate_tasks",
    "call delegate_tasks",
    "at least one proposal",
    "at least two workers",
    "never work directly",
    "delegation is required",
)

#: Strings that would hand the model the C2 design, a task identity or a prepared assignment.
LEAK_TOKENS: tuple[str, ...] = (
    "closure2",
    "stage-c2",
    "test_public",
    "test_hidden",
    "[your assignment",
    "coding_worker",  # the arm discloses the profile at run time; the prompt stays profile-agnostic
    "pipeline/",
    "lifecycle/",
    "plugins/",
    "benchmark",
    "experiment",
    "natural delegation",
)


def _replace(text: str, entry: dict[str, str]) -> str:
    start, end = entry["start"], entry["end"]
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"{entry['id']}: the section boundaries are not unique in the base text")
    left, right = text.index(start), text.index(end)
    if right < left:
        raise ValueError(f"{entry['id']}: the section boundaries are in an unexpected order")
    return text[:left] + entry["replacement"] + text[right:]


def build_text() -> str:
    """Apply the three replacements to B0, in the declared order."""

    text = B0_TEXT
    for entry in REPLACEMENTS:
        text = _replace(text, entry)
    return text


V30_TEXT = build_text()
V30_DIGEST = "sha256:" + hashlib.sha256(V30_TEXT.encode("utf-8")).hexdigest()


def digest_of(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def section_headings(text: str) -> tuple[str, ...]:
    headings = (line.strip() for line in text.splitlines() if line.startswith("## "))
    return tuple(line for line in headings if not line.startswith("### "))


def sections(text: str) -> dict[str, str]:
    """Every ``## `` section body, keyed by heading, in file order."""

    lines = text.splitlines(keepends=True)
    out: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in lines:
        if line.startswith("## ") and not line.startswith("### "):
            if current is not None:
                out[current] = "".join(buffer)
            current = line.strip()
            buffer = []
            continue
        if current is not None:
            buffer.append(line)
    if current is not None:
        out[current] = "".join(buffer)
    return out


def change_records() -> list[dict[str, str]]:
    """The before/after of each replacement, for review and for the frozen record."""

    before = sections(B0_TEXT)
    after = sections(V30_TEXT)
    records: list[dict[str, str]] = []
    for entry in REPLACEMENTS:
        old = before.get(entry["start"], "")
        new = after.get(entry["start"], "")
        records.append(
            {
                "id": entry["id"],
                "section": entry["start"],
                "problem": entry["problem"],
                "change": entry["change"],
                "before_chars": str(len(old)),
                "after_chars": str(len(new)),
                "before_digest": digest_of(old),
                "after_digest": digest_of(new),
                "before": old,
                "after": new,
            }
        )
    return records


def changed_sections() -> tuple[str, ...]:
    before = sections(B0_TEXT)
    after = sections(V30_TEXT)
    return tuple(name for name in before if before[name] != after.get(name))


def unchanged_sections() -> dict[str, bool]:
    """Every section the revision promises not to touch, compared byte for byte."""

    before = sections(B0_TEXT)
    after = sections(V30_TEXT)
    replaced = {entry["start"] for entry in REPLACEMENTS}
    return {name: before[name] == after.get(name) for name in before if name not in replaced}


def validate() -> list[str]:
    """Every promise this file makes about B0 and B1, checked on the bytes."""

    problems: list[str] = []

    if B0_TEXT != c1b_prompts.V22_TEXT or B0_DIGEST != c1b_prompts.V22_DIGEST:
        problems.append("B0 is not the Stage C1-B prompt bytes")
    if digest_of(B0_TEXT) != B0_DIGEST:
        problems.append("B0 does not hash to its own digest")
    if B0_VERSION != c1b_prompts.V22_VERSION:
        problems.append("B0 claims a different version from Stage C1-B")

    if section_headings(V30_TEXT) != B0_SECTIONS:
        problems.append("B1 does not have the same headings in the same order as B0")
    if section_headings(B0_TEXT) != B0_SECTIONS:
        problems.append("B0 no longer has the heading list this module declares")

    changed = changed_sections()
    expected = tuple(entry["start"] for entry in REPLACEMENTS)
    if changed != expected:
        problems.append(f"B1 changes {list(changed)}, designed to change {list(expected)}")

    unchanged = unchanged_sections()
    for name, same in unchanged.items():
        if not same:
            problems.append(f"B1 changed the untouched section {name!r}")

    # C1-B's replacement must survive byte for byte: it is the section the C1 verification loop
    # proved necessary, and a decision-policy edit is not a reason to reopen it.
    keeping_start = V30_TEXT.index("## TESTING AND RECOVERY")
    keeping_end = V30_TEXT.index("## FINAL DELIVERY")
    keeping = V30_TEXT[keeping_start:keeping_end].strip("\n")
    c1b_section = c1b_prompts.changed_sections()["replacement"].strip("\n")
    if keeping != c1b_section:
        problems.append("B1 does not keep the C1-B TESTING AND RECOVERY section byte for byte")

    if V30_DIGEST == B0_DIGEST:
        problems.append("B1 is byte-identical to B0, so the decision policy did not change")
    if digest_of(V30_TEXT) != V30_DIGEST:
        problems.append("B1 does not hash to its own digest")
    if not len(B0_TEXT) < len(V30_TEXT) <= int(len(B0_TEXT) * 1.5):
        problems.append(
            f"B1 is {len(V30_TEXT)} chars against B0's {len(B0_TEXT)}; a minimal revision must "
            "grow by less than half"
        )

    lowered = V30_TEXT.lower()
    for phrase in MANDATE_PHRASES:
        if phrase in lowered:
            problems.append(f"B1 contains the mandate phrase {phrase!r}")
    for token in LEAK_TOKENS:
        if token in lowered:
            problems.append(f"B1 contains the task or design token {token!r}")

    for entry in REPLACEMENTS:
        if not entry["replacement"].startswith(entry["start"]):
            problems.append(f"{entry['id']} does not start with the heading it replaces")
        if not entry["replacement"].endswith("\n\n"):
            problems.append(f"{entry['id']} does not end with a blank line before the next heading")
        if not entry["problem"].strip() or not entry["change"].strip():
            problems.append(f"{entry['id']} does not state its problem and its change")

    return problems


__all__ = [
    "B0_DIGEST",
    "B0_SECTIONS",
    "B0_TEXT",
    "B0_VERSION",
    "LEAK_TOKENS",
    "MANDATE_PHRASES",
    "REPLACEMENTS",
    "V30_DIGEST",
    "V30_TEXT",
    "V30_VERSION",
    "build_text",
    "change_records",
    "changed_sections",
    "digest_of",
    "section_headings",
    "sections",
    "unchanged_sections",
    "validate",
]
