"""Closure-2A-R1: the frozen benchmark definition and the registration identity.

Structural and pure-function checks only — no subprocess, no router decision, no model. The
executable checks (seed fails, oracle executes, router decisions, leaked answers) live in
``tests/integration/test_closure2_preflight.py``.

The R1 repair files this suite guards:

* R1-01 approval state is not part of the frozen definition;
* R1-02 the registration digest does not depend on where the repository is checked out;
* R1-03 the arm digest is bound to the effective YAML content, not the file name.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from experiments.evaluation.closure2 import protocol
from experiments.evaluation.closure2.catalog import (
    BENCHMARK_VERSION,
    TASKS,
    build_task,
    task_digests,
)
from experiments.evaluation.closure2.protocol import (
    ARMS,
    BUDGET,
    C2A_FROZEN_TASK_DIGESTS,
    C2A_HEAD_COMMIT,
    C2A_PREREGISTRATION_DIGEST,
    C2A_R1_PREREGISTRATION_DIGEST,
    COMPARISONS,
    EXCLUSIONS,
    LEGACY_CONTEXT_CONFIG,
    METRICS,
    REPO_ROOT,
    SAME_DIGEST_GROUP,
    SHARED_PREFIX_CONFIG,
    UNSUPPORTED,
    approval_gate,
    arm_config_digest,
    config_semantic_digest,
    layout_ablation_delta,
    preregistration_digest,
    preregistration_payload,
    require_execution_approval,
    resolve_repo_path,
    task_taskspec_parity,
)
from experiments.evaluation.models import canonical_digest

TASK_IDS = {task.task_id for task in TASKS}
FAMILIES = {task.family for task in TASKS}

#: The C2-A identity, written out as a literal so a test cannot agree with a wrong constant.
C2A_LITERAL_DIGEST = "f7bbdfb8350589a959d78be00a9dc150c3b12fd6d277cf0fe2cee8a8b536b058"

#: A syntactically complete approval record naming an arbitrary digest.
APPROVED_TEMPLATE: dict[str, Any] = {
    "status": "APPROVED",
    "approved_digest": None,
    "approved_by": "owner",
    "approval_commit": "0" * 40,
    "approved_at": "2026-11-17T00:00:00+00:00",
}


def copy_frozen_configs(root: Path) -> None:
    """A second checkout of the same content, without copying the repository."""

    for relative in (LEGACY_CONTEXT_CONFIG, SHARED_PREFIX_CONFIG):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / relative, target)


def read_config(relative: str) -> str:
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def write_config(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


def test_the_benchmark_has_exactly_three_tasks_in_three_families() -> None:
    expected = {
        "closure2-pipeline-migration",
        "closure2-plugin-migration",
        "closure2-state-machine-refactor",
    }
    assert expected == TASK_IDS
    assert {"P1", "P2", "N1"} == FAMILIES


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_every_task_is_a_migration_of_a_working_seed(task) -> None:
    assert task.seed, "a task needs a v1 seed"
    assert task.reference, "a task needs a reference implementation"
    assert task.oracle_tests, "a task needs a hidden oracle"
    assert task.public_tests, "a task needs public tests"
    # The reference only replaces files that already exist in the seed: a migration, not a scaffold.
    assert set(task.reference) <= set(task.seed)
    # Every deliverable is a real seeded file.
    assert set(task.deliverables) <= set(task.seed)


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_hidden_oracle_never_ships_in_the_workspace(task) -> None:
    visible = build_task(task, topology="multi").visible_files
    for name in task.oracle_tests:
        assert name not in visible


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_two_topologies_describe_the_same_task(task) -> None:
    single = build_task(task, topology="single")
    multi = build_task(task, topology="multi")

    assert single.visible_files == multi.visible_files
    assert single.hidden_files == multi.hidden_files
    assert single.required_artifacts == multi.required_artifacts
    assert single.allowed_paths == multi.allowed_paths
    assert single.forbidden_paths == multi.forbidden_paths
    assert all(step.shared_context == task.shared_context for step in multi.workers)
    assert task.shared_context.splitlines()[0] in single.prompt
    assert len(single.workers) == 1
    assert len(multi.workers) >= 2


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_digest_is_stable_and_versioned(task) -> None:
    assert (
        build_task(task, topology="multi").effective_public_task_digest
        == build_task(task, topology="multi").effective_public_task_digest
    )
    digests = task_digests(task)
    assert set(digests) >= {
        "seed_digest",
        "reference_digest",
        "oracle_digest",
        "public_requirement_digest",
        "candidate_dag_digest",
        "effective_public_task_digest_single",
        "effective_public_task_digest_multi",
    }
    assert (
        digests["effective_public_task_digest_single"]
        != digests["effective_public_task_digest_multi"]
    )


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.task_id)
def test_the_frozen_task_content_matches_the_independent_c2a_identity(task) -> None:
    """The baseline is the C2-A table, not a digest recomputed from the live objects (R1-05)."""

    expected = C2A_FROZEN_TASK_DIGESTS[task.task_id]
    actual = task_digests(task)
    for key, value in expected.items():
        assert actual[key] == value, f"{task.task_id}:{key} moved since Closure-2A"


def test_a_tampered_seed_breaks_the_frozen_identity() -> None:
    task = TASKS[0]
    tampered = dataclasses.replace(
        task, seed={**task.seed, "pipeline/parsing.py": "# not the frozen seed\n"}
    )
    actual = task_digests(tampered)["seed_digest"]
    assert actual != C2A_FROZEN_TASK_DIGESTS[task.task_id]["seed_digest"]


def test_same_topology_arms_agree_on_the_digest() -> None:
    parity = task_taskspec_parity()
    for task in TASKS:
        single = build_task(task, topology="single").effective_public_task_digest
        for arm, entry in ARMS.items():
            if entry["topology"] != "single":
                continue
            assert build_task(task, topology="single").effective_public_task_digest == single, (
                f"{arm} disagrees with another single arm on {task.task_id}"
            )
        for group in SAME_DIGEST_GROUP:
            digests = {
                build_task(task, topology=ARMS[arm]["topology"]).effective_public_task_digest
                for arm in group
            }
            assert len(digests) == 1, f"{group} disagree on {task.task_id}"
        assert parity[task.task_id]["same_visible_files"] is True


def test_the_arm_matrix_matches_the_preregistered_protocol() -> None:
    assert set(ARMS) == {"N0", "N1", "A0", "A1-L", "A1-P", "A2-P"}
    assert ARMS["N0"]["topology"] == "single"
    assert ARMS["A0"]["topology"] == "single"
    for arm in ("N1", "A1-L", "A1-P", "A2-P"):
        assert ARMS[arm]["topology"] == "multi"
    left, right = ARMS["A1-L"], ARMS["A1-P"]
    differing = {key for key in left if left[key] != right[key]}
    assert differing == {"prompt_layout", "config"}
    assert (left["prompt_layout"], right["prompt_layout"]) == ("legacy", "shared_prefix")
    assert ARMS["A2-P"]["route"] == "selective"
    assert ARMS["A1-P"]["route"] == "off"


def test_every_arm_config_digest_is_unique() -> None:
    digests = {arm_config_digest(arm) for arm in ARMS}
    assert len(digests) == len(ARMS)


# ------------------------------------------------------------------ R1-01: budget vs approval


def test_the_budget_carries_parameters_only() -> None:
    single, multi = BUDGET["single"], BUDGET["multi"]
    assert multi["max_turns_per_worker"] * multi["workers"] == single["max_turns"] == 36
    assert single["token_budget"] == multi["token_budget"]
    assert 240_000 <= single["token_budget"] <= 300_000
    assert single["max_tool_calls"] == multi["max_tool_calls"]
    assert "approval" not in BUDGET
    assert "status" not in BUDGET


def test_the_approval_state_is_not_in_the_frozen_payload() -> None:
    """The record is owner state outside the preregistration: neither its status nor its digest.

    Asserting what the record currently *says* would make this test a status report; asserting what
    the payload can never contain keeps it an invariant.
    """

    payload = preregistration_payload()
    assert "approval" not in payload
    assert "status" not in payload
    assert "approved_digest" not in payload


def test_the_frozen_digest_does_not_depend_on_the_approval_record(monkeypatch) -> None:
    before = preregistration_digest()
    monkeypatch.setattr(
        protocol,
        "APPROVAL_RECORD",
        {**APPROVED_TEMPLATE, "approved_digest": before},
    )
    assert preregistration_digest() == before
    assert approval_gate()["execution_ready"] is True


def test_changing_a_budget_parameter_changes_the_frozen_digest(monkeypatch) -> None:
    before = preregistration_digest()
    arm_before = arm_config_digest("N0")
    monkeypatch.setitem(protocol.BUDGET["single"], "token_budget", 250_000)
    assert preregistration_digest() != before
    assert arm_config_digest("N0") != arm_before


PENDING_TEMPLATE: dict[str, Any] = {
    "status": "PENDING_OWNER_APPROVAL",
    "approved_digest": None,
    "approved_by": None,
    "approval_commit": None,
    "approved_at": None,
}


def test_a_pending_record_blocks_execution() -> None:
    gate = approval_gate(record=PENDING_TEMPLATE)
    assert gate["execution_ready"] is False
    assert any("not 'APPROVED'" in problem for problem in gate["problems"])
    with pytest.raises(PermissionError):
        require_execution_approval(record=PENDING_TEMPLATE)


def test_an_approval_of_the_wrong_digest_fails_closed() -> None:
    gate = approval_gate(record={**APPROVED_TEMPLATE, "approved_digest": "0" * 64})
    assert gate["execution_ready"] is False
    assert any("names digest" in problem for problem in gate["problems"])
    with pytest.raises(PermissionError):
        require_execution_approval(record={**APPROVED_TEMPLATE, "approved_digest": "0" * 64})


@pytest.mark.parametrize("missing", ["approved_by", "approval_commit", "approved_at"])
def test_an_approval_without_evidence_fails_closed(missing: str) -> None:
    record = {**APPROVED_TEMPLATE, "approved_digest": preregistration_digest(), missing: None}
    gate = approval_gate(record=record)
    assert gate["execution_ready"] is False
    assert gate["problems"]


def test_a_correct_approval_is_accepted() -> None:
    record = {**APPROVED_TEMPLATE, "approved_digest": preregistration_digest()}
    assert approval_gate(record=record)["execution_ready"] is True
    assert require_execution_approval(record=record)["execution_ready"] is True


# ------------------------------------------------- R2-01: approval binds the frozen identity


def test_r2_01_a_correct_approval_of_the_frozen_digest_is_accepted() -> None:
    """TEST A — frozen identity matches, so a complete, correct approval is usable."""

    record = {**APPROVED_TEMPLATE, "approved_digest": C2A_R1_PREREGISTRATION_DIGEST}
    gate = approval_gate(record=record)
    assert gate["frozen_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert gate["current_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert gate["execution_ready"] is True
    assert gate["problems"] == []


def test_r2_01_approving_a_drifted_budget_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST C — re-signing the drift must not turn a changed budget into an approved benchmark."""

    monkeypatch.setitem(protocol.BUDGET["single"], "token_budget", 280_000)
    monkeypatch.setitem(protocol.BUDGET["multi"], "token_budget", 280_000)
    drifted = preregistration_digest()
    assert drifted != C2A_R1_PREREGISTRATION_DIGEST

    # A perfectly self-consistent signature: the approval names what the repository says right now.
    record = {**APPROVED_TEMPLATE, "approved_digest": drifted}
    gate = approval_gate(record=record)

    assert gate["current_digest"] == gate["approved_digest"] == drifted
    assert gate["execution_ready"] is False
    assert any("frozen definition moved" in problem for problem in gate["problems"])
    assert any("frozen R1 digest" in problem for problem in gate["problems"])
    with pytest.raises(PermissionError, match="frozen definition moved"):
        require_execution_approval(record=record)


def test_r2_01_approving_a_drifted_config_is_refused(tmp_path: Path) -> None:
    """TEST D — a drifted YAML approved by its own digest is still not the R1 registration."""

    copy_frozen_configs(tmp_path)
    text = read_config(SHARED_PREFIX_CONFIG)
    drifted_text = text.replace("max_results: 5", "max_results: 6")
    assert drifted_text != text
    write_config(tmp_path, SHARED_PREFIX_CONFIG, drifted_text)

    drifted = preregistration_digest(root=tmp_path)
    assert drifted != C2A_R1_PREREGISTRATION_DIGEST
    record = {**APPROVED_TEMPLATE, "approved_digest": drifted}

    gate = approval_gate(record=record, root=tmp_path)
    assert gate["current_digest"] == drifted
    assert gate["execution_ready"] is False
    assert any("frozen definition moved" in problem for problem in gate["problems"])


def test_r2_01_the_frozen_identity_still_approves_from_another_checkout(tmp_path: Path) -> None:
    """The frozen check must not become a path check: identical content approves from elsewhere."""

    copy_frozen_configs(tmp_path)
    record = {**APPROVED_TEMPLATE, "approved_digest": C2A_R1_PREREGISTRATION_DIGEST}
    gate = approval_gate(record=record, root=tmp_path)
    assert gate["current_digest"] == C2A_R1_PREREGISTRATION_DIGEST
    assert gate["execution_ready"] is True


def test_r2_01_the_gate_accepts_no_digest_override() -> None:
    """An override would let a caller pass the frozen value while the repository says otherwise."""

    assert "digest" not in inspect.signature(approval_gate).parameters
    assert "digest" not in inspect.signature(require_execution_approval).parameters
    with pytest.raises(TypeError):
        approval_gate(**{"record": APPROVED_TEMPLATE, "digest": C2A_R1_PREREGISTRATION_DIGEST})
    with pytest.raises(TypeError):
        require_execution_approval(
            **{"record": APPROVED_TEMPLATE, "digest": C2A_R1_PREREGISTRATION_DIGEST}
        )


def test_r2_01_a_pending_record_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """TEST F — the refusal is a property of the record, so a pending one is installed explicitly.

    The live record is an owner decision and no longer pending; the behaviour under test is that a
    pending record cannot authorize, whatever the checkout's current decision happens to be.
    """

    monkeypatch.setattr(
        protocol,
        "APPROVAL_RECORD",
        {
            "status": "PENDING_OWNER_APPROVAL",
            "approved_digest": None,
            "approved_by": None,
            "approval_commit": None,
            "approved_at": None,
        },
    )
    gate = approval_gate()
    assert gate["execution_ready"] is False
    assert gate["status"] == "PENDING_OWNER_APPROVAL"
    assert gate["approved_digest"] is None
    with pytest.raises(PermissionError):
        require_execution_approval()


def test_r2_01_the_live_gate_reports_the_live_record() -> None:
    """Whatever the owner decided, the gate reports it and nothing else."""

    gate = approval_gate()
    assert gate["status"] == protocol.APPROVAL_RECORD["status"]
    assert gate["approved_digest"] == protocol.APPROVAL_RECORD["approved_digest"]
    assert gate["execution_ready"] is (protocol.APPROVAL_RECORD["status"] == "APPROVED")


# ------------------------------------------------------------------ R1-02: no checkout path


def test_the_frozen_config_paths_are_repository_relative() -> None:
    for relative in (LEGACY_CONTEXT_CONFIG, SHARED_PREFIX_CONFIG):
        assert isinstance(relative, str)
        assert not Path(relative).is_absolute()
        assert ":" not in relative
        assert ".." not in Path(relative).parts


def test_a_relative_path_resolves_against_the_repository_root() -> None:
    resolved = resolve_repo_path(LEGACY_CONTEXT_CONFIG)
    assert resolved.is_absolute()
    assert resolved == REPO_ROOT / LEGACY_CONTEXT_CONFIG
    assert resolved.is_file()


def _make_dir_link(target: Path, link: Path) -> str:
    """Create a directory link, preferring a symlink and falling back to a Windows junction."""

    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pass
    else:
        return "symlink"

    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and link.exists():
        return "junction"
    return "none"


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "..\\outside.yaml",
        "experiments\\..\\..\\outside.yaml",
        "../outside.yaml",
        "experiments/../../x.yaml",
        "\\\\server\\share\\file.yaml",
        "//server/share/file.yaml",
        "",
    ],
)
def test_escaping_paths_are_refused(candidate: str) -> None:
    with pytest.raises((ValueError, TypeError)):
        resolve_repo_path(candidate)


def test_r2_02_a_backslash_path_never_reaches_the_filesystem(tmp_path: Path) -> None:
    """Windows treats ``\\`` as a separator; POSIX validation treats it as an ordinary letter."""

    for candidate in (
        r"..\outside.yaml",
        r"experiments\..\..\outside.yaml",
        r"\\server\share\file.yaml",
        r"experiments\configs\final4-context.yaml",
    ):
        with pytest.raises(ValueError, match="separator"):
            resolve_repo_path(candidate, root=tmp_path)


def test_r2_02_a_drive_prefix_is_refused_even_with_forward_slashes() -> None:
    with pytest.raises(ValueError, match="drive or stream prefix"):
        resolve_repo_path("C:/Windows/win.ini")
    with pytest.raises(ValueError, match="drive or stream prefix"):
        resolve_repo_path("C:relative/inside.yaml")


def test_r2_02_a_legitimate_path_resolves_under_each_root(tmp_path: Path) -> None:
    first = tmp_path / "checkout-one"
    second = tmp_path / "checkout-two"
    copy_frozen_configs(first)
    copy_frozen_configs(second)
    for root in (first, second):
        resolved = resolve_repo_path(SHARED_PREFIX_CONFIG, root=root)
        assert resolved.parent.parent.parent == root
        assert resolved.is_file()


def test_r2_02_a_link_that_leaves_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    kind = _make_dir_link(outside, link)
    if kind == "none":
        pytest.skip("this host cannot create a symlink or directory junction without elevation")
    with pytest.raises(ValueError, match="outside the repository root"):
        resolve_repo_path("escape/leak.yaml", root=root)


def test_r2_02_a_sibling_tree_with_the_same_prefix_is_not_inside(tmp_path: Path) -> None:
    """``repo-other`` shares a string prefix with ``repo`` yet is a different tree.

    A ``startswith`` check would accept it; ``Path.is_relative_to`` after ``resolve()`` does not.
    The sibling is reachable only through a link, which is exactly the shape being refused.
    """

    root = tmp_path / "repo"
    sibling = tmp_path / "repo-other"
    root.mkdir()
    sibling.mkdir()
    kind = _make_dir_link(sibling, root / "sibling")
    if kind == "none":
        pytest.skip("this host cannot create a symlink or directory junction without elevation")
    with pytest.raises(ValueError, match="outside the repository root"):
        resolve_repo_path("sibling/file.yaml", root=root)


def test_r2_02_path_checks_do_not_change_the_frozen_identity() -> None:
    """Validating a path must not rewrite a config, and must not move any digest."""

    before = preregistration_digest()
    shared_before = (REPO_ROOT / SHARED_PREFIX_CONFIG).read_text(encoding="utf-8")
    for _ in range(3):
        resolve_repo_path(LEGACY_CONTEXT_CONFIG)
        resolve_repo_path(SHARED_PREFIX_CONFIG)
    assert (REPO_ROOT / SHARED_PREFIX_CONFIG).read_text(encoding="utf-8") == shared_before
    assert preregistration_digest() == before == C2A_R1_PREREGISTRATION_DIGEST
    assert config_semantic_digest(LEGACY_CONTEXT_CONFIG) == (
        "16f587b3696e5d4e9f9a7868aa0859c89da876975ef7060f8a2936ce72899eb8"
    )
    assert config_semantic_digest(SHARED_PREFIX_CONFIG) == (
        "751beb1506602e250f240f69771514c7c2b569e3903c01060a554455dcfc549a"
    )


def test_the_registration_digest_is_identical_in_two_checkouts(tmp_path: Path) -> None:
    """R1-02: the same Git content must fingerprint the same from any directory."""

    first = tmp_path / "checkout-one"
    second = tmp_path / "elsewhere" / "checkout-two"
    copy_frozen_configs(first)
    copy_frozen_configs(second)

    assert preregistration_digest(root=first) == preregistration_digest(root=second)
    assert preregistration_digest(root=first) == C2A_R1_PREREGISTRATION_DIGEST


def test_the_frozen_payload_contains_no_absolute_path() -> None:
    def walk(node, trail=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, f"{trail}.{key}" if trail else str(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{trail}[{index}]")
        elif isinstance(node, str) and (node.startswith("/") or len(node) > 2 and node[1] == ":"):
            yield f"{trail}={node}"

    offenders = [
        item for item in walk(preregistration_payload()) if "/workspace/project" not in item
    ]
    assert offenders == []


# ------------------------------------------------------------------ R1-03: content-bound digest


def test_the_config_digest_binds_the_effective_content(tmp_path: Path) -> None:
    copy_frozen_configs(tmp_path)
    baseline = config_semantic_digest(SHARED_PREFIX_CONFIG, root=tmp_path)
    assert baseline == config_semantic_digest(SHARED_PREFIX_CONFIG)

    text = read_config(SHARED_PREFIX_CONFIG)
    moved = text.replace("model: deepseek-chat", "model: deepseek-reasoner")
    assert moved != text
    write_config(tmp_path, SHARED_PREFIX_CONFIG, moved)

    assert config_semantic_digest(SHARED_PREFIX_CONFIG, root=tmp_path) != baseline
    assert arm_config_digest("A1-P", root=tmp_path) != arm_config_digest("A1-P")


def test_a_comment_only_edit_keeps_the_arm_digest(tmp_path: Path) -> None:
    copy_frozen_configs(tmp_path)
    text = read_config(SHARED_PREFIX_CONFIG)
    write_config(
        tmp_path,
        SHARED_PREFIX_CONFIG,
        "# a comment that must not change the identity\n" + text + "\n\n",
    )
    assert config_semantic_digest(SHARED_PREFIX_CONFIG, root=tmp_path) == config_semantic_digest(
        SHARED_PREFIX_CONFIG
    )
    assert arm_config_digest("A1-P", root=tmp_path) == arm_config_digest("A1-P")


def test_a_numeric_formatting_change_keeps_the_arm_digest(tmp_path: Path) -> None:
    """A formatter rewriting ``0.006`` as ``0.006000`` must not change the identity."""

    copy_frozen_configs(tmp_path)
    text = read_config(SHARED_PREFIX_CONFIG)
    rewritten = text.replace(
        "cache_hit_input_per_million: 0.006", "cache_hit_input_per_million: 0.006000"
    )
    assert rewritten != text
    write_config(tmp_path, SHARED_PREFIX_CONFIG, rewritten)
    assert config_semantic_digest(SHARED_PREFIX_CONFIG, root=tmp_path) == config_semantic_digest(
        SHARED_PREFIX_CONFIG
    )


def test_an_integral_float_and_an_int_are_one_effective_value(tmp_path: Path) -> None:
    """``0.0`` and ``0`` are the same configuration; the digest must agree."""

    left = tmp_path / "as-float"
    right = tmp_path / "as-int"
    write_config(left, SHARED_PREFIX_CONFIG, "temperature: 0.0\nmax_results: 5\n")
    write_config(right, SHARED_PREFIX_CONFIG, "temperature: 0\nmax_results: 5\n")
    assert config_semantic_digest(SHARED_PREFIX_CONFIG, root=left) == config_semantic_digest(
        SHARED_PREFIX_CONFIG, root=right
    )


def test_the_layout_ablation_is_one_variable_in_the_frozen_files() -> None:
    ablation = layout_ablation_delta()
    assert ablation["differing_keys"] == ["context.prompt_layout"]
    assert ablation["only_layout_differs"] is True
    # The legacy file does not declare the key: "legacy" is the resolved default, which the
    # executable layout check verifies through ``load_config`` rather than by string inspection.
    assert ablation["left_layout"] is None
    assert ablation["right_layout"] == "shared_prefix"


@pytest.mark.parametrize(
    ("relative", "needle", "replacement"),
    [
        (SHARED_PREFIX_CONFIG, "max_results: 5", "max_results: 6"),
        (SHARED_PREFIX_CONFIG, "image: atlas-uat-python:3.12", "image: other-image:3.12"),
        (LEGACY_CONTEXT_CONFIG, "search_backend: keyword", "search_backend: embedding"),
    ],
)
def test_a_second_effective_difference_breaks_layout_parity(
    tmp_path: Path, relative: str, needle: str, replacement: str
) -> None:
    copy_frozen_configs(tmp_path)
    text = read_config(relative)
    skewed = text.replace(needle, replacement)
    assert skewed != text, f"the frozen config no longer contains {needle!r}"
    write_config(tmp_path, relative, skewed)

    ablation = layout_ablation_delta(root=tmp_path)
    assert ablation["only_layout_differs"] is False
    assert "context.prompt_layout" in ablation["differing_keys"]


def test_the_two_configs_are_distinct_files_with_the_declared_layout() -> None:
    legacy = yaml.safe_load(read_config(LEGACY_CONTEXT_CONFIG))
    shared = yaml.safe_load(read_config(SHARED_PREFIX_CONFIG))
    assert legacy["context"].get("prompt_layout", "legacy") == "legacy"
    assert shared["context"]["prompt_layout"] == "shared_prefix"
    assert LEGACY_CONTEXT_CONFIG != SHARED_PREFIX_CONFIG


# ------------------------------------------------------------------ registration identity


def test_the_registration_digest_is_the_pinned_r1_identity() -> None:
    assert preregistration_digest() == C2A_R1_PREREGISTRATION_DIGEST
    assert preregistration_digest() == canonical_digest(preregistration_payload())
    assert C2A_R1_PREREGISTRATION_DIGEST != C2A_PREREGISTRATION_DIGEST


def test_the_c2a_identity_is_preserved_as_history() -> None:
    assert C2A_PREREGISTRATION_DIGEST == C2A_LITERAL_DIGEST
    payload = preregistration_payload()
    assert payload["supersedes"] == {
        "protocol_revision": "closure-2a",
        "preregistration_digest": C2A_LITERAL_DIGEST,
        "task_commit": "7f5c1eae12e466e6a37ce2a7d77608ba65a009a5",
        "head_commit": C2A_HEAD_COMMIT,
    }
    assert payload["protocol_revision"] == "closure-2a-r1"


def test_the_preregistration_payload_is_complete() -> None:
    payload = preregistration_payload()
    assert payload["benchmark_version"] == BENCHMARK_VERSION
    assert len(payload["tasks"]) == len(TASKS)
    assert payload["release_boundary"]["rule"]
    assert "NOT" in payload["release_boundary"]["repository_contains"].upper()
    assert preregistration_digest() == preregistration_digest()


def test_the_protocol_names_what_it_cannot_compare() -> None:
    assert UNSUPPORTED
    assert any("Router" in item for item in UNSUPPORTED)
    assert any("Context Plane" in item for item in UNSUPPORTED)
    assert {item["id"] for item in COMPARISONS} >= {
        "native-vs-atlas-single",
        "native-vs-atlas-multi",
        "layout-ablation",
        "router-admission",
    }
    assert EXCLUSIONS["never_allowed"]
    assert "oracle" in METRICS["primary"]
