"""Stage B+ capability disclosure: derived, truthful, and removable.

The card is the only place the Main can learn what a worker can do, so the tests are about two
failure modes that would make an ablation meaningless: a disclosure that claims a tool the profile
does not have, and a disclosure that is hand-maintained and therefore drifts from the grant.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlasos.delegation.capability_card import (
    CARD_AUTHORITY,
    REFERENCE_SURFACE,
    build_card,
)
from atlasos.delegation.exposure import (
    LEVELS,
    WorkerExposureLevel,
    exposure_evidence,
    render_exposure,
)
from atlasos.delegation.tool import DEFAULT_DESCRIPTION
from atlasos.workers import WorkerCatalog
from atlasos.workers.factory import DEFAULT_AVAILABLE_TOOLS

SHIPPED = WorkerCatalog()
RESEARCH = SHIPPED.profiles["research_worker"]
CODING = SHIPPED.profiles["coding_worker"]


def card(profile=RESEARCH, **kwargs: Any):
    defaults = {
        "registered_tools": DEFAULT_AVAILABLE_TOOLS,
        "surface_tools": REFERENCE_SURFACE,
        "granted_write_paths": (),
        "child_max_turns": 20,
        "result_preview_chars": 900,
    }
    defaults.update(kwargs)
    return build_card(profile, **defaults)


def test_none_returns_the_shipped_bytes_object() -> None:
    """An ablation that asks for nothing must not move the shipped description."""

    assert render_exposure("none", (card(),), base_description=DEFAULT_DESCRIPTION) is (
        DEFAULT_DESCRIPTION
    )


def test_none_with_no_cards_is_still_the_shipped_bytes() -> None:
    assert render_exposure("none", (), base_description=DEFAULT_DESCRIPTION) == DEFAULT_DESCRIPTION


@pytest.mark.parametrize("level", ["native", "card"])
def test_disclosure_appends_after_the_shipped_description(level: str) -> None:
    rendered = render_exposure(level, (card(),), base_description=DEFAULT_DESCRIPTION)
    assert rendered.startswith(DEFAULT_DESCRIPTION)
    assert len(rendered) > len(DEFAULT_DESCRIPTION)


def test_native_disclosure_never_offers_a_tool_the_profile_lacks() -> None:
    rendered = render_exposure("native", (card(RESEARCH),), base_description=DEFAULT_DESCRIPTION)
    allowed_line = next(
        line for line in rendered.splitlines() if line.strip().startswith("Can use")
    )
    for tool in ("file_write", "file_edit", "bash", "run_public_tests"):
        assert tool not in allowed_line
    missing_line = next(
        line for line in rendered.splitlines() if line.strip().startswith("Does not have")
    )
    for tool in ("file_write", "file_edit", "bash", "run_public_tests", "delegate_tasks"):
        assert tool in missing_line


def test_card_disclosure_lists_the_real_tool_sets_by_name() -> None:
    rendered = render_exposure("card", (card(CODING),), base_description=DEFAULT_DESCRIPTION)
    allowed_block = rendered.split("allowed_capabilities:", 1)[1].split(
        "forbidden_capabilities:", 1
    )[0]
    for tool in CODING.base_tools:
        assert f"- {tool}" in allowed_block
    forbidden_block = rendered.split("forbidden_capabilities:", 1)[1].split(
        "conditional_capabilities:", 1
    )[0]
    assert "- run_public_tests" in forbidden_block
    assert "- delegate_tasks" in forbidden_block


def test_research_card_is_read_only_and_says_why() -> None:
    built = card(RESEARCH)
    assert built.read_only
    assert set(built.allowed) == set(RESEARCH.base_tools)
    assert "file_write" in built.not_granted
    assert "run_public_tests" in built.not_granted
    assert any("read-only profile" in line for line in built.conditional)
    assert built.max_turns == min(RESEARCH.max_turns, 20)


def test_coding_card_with_a_grant_names_the_grant() -> None:
    built = card(CODING, granted_write_paths=("pipeline/",))
    assert not built.read_only
    assert set(built.allowed) == set(CODING.base_tools)
    assert any("pipeline/" in line for line in built.conditional)
    assert not any("no write paths" in line for line in built.conditional)


def test_coding_card_without_a_grant_states_the_effective_restriction() -> None:
    built = card(CODING)
    assert any("no write paths" in line for line in built.conditional)


def test_card_tracks_the_grant_rather_than_a_constant() -> None:
    """The digest must move when the grant moves; otherwise it is a hardcoded list."""

    without = card(CODING)
    with_grant = card(CODING, granted_write_paths=("pipeline/",))
    assert without.digest() != with_grant.digest()


def test_card_reports_the_effective_turn_cap_not_the_requested_one() -> None:
    """``min(profile.max_turns, child_max_turns)`` is what the child agent receives.

    The shipped coding profile caps at 8, so a run that asks for 20 gets 8 and the card must say 8 —
    this is the S07 finding, and a disclosure that reported 20 would be false.
    """

    assert CODING.max_turns == 8
    assert card(CODING, child_max_turns=6).max_turns == 6
    assert card(CODING, child_max_turns=20).max_turns == 8
    # Raising the profile is the only way to raise the effective cap.
    raised = CODING.model_copy(update={"max_turns": 20})
    assert card(raised, child_max_turns=20).max_turns == 20


def test_card_withholds_a_tool_the_run_does_not_register() -> None:
    built = card(RESEARCH, registered_tools={"file_read", "grep"})
    assert set(built.allowed) == {"file_read", "grep"}
    assert any("withheld" in line for line in built.conditional)


def test_card_declares_it_confers_no_authority() -> None:
    payload = card().as_dict()
    assert payload["authority"] == CARD_AUTHORITY
    assert CARD_AUTHORITY == "disclosure_only"


def test_card_render_is_deterministic() -> None:
    first = render_exposure("card", (card(RESEARCH), card(CODING)), base_description="base")
    second = render_exposure("card", (card(RESEARCH), card(CODING)), base_description="base")
    assert first == second


def test_unknown_exposure_level_is_refused() -> None:
    with pytest.raises(ValueError):
        render_exposure("verbose", (card(),), base_description=DEFAULT_DESCRIPTION)
    assert set(LEVELS) == {level.value for level in WorkerExposureLevel}


def test_evidence_reports_the_delta_not_the_constant() -> None:
    rendered = render_exposure("card", (card(),), base_description=DEFAULT_DESCRIPTION)
    evidence = exposure_evidence(
        "card", rendered, (card(),), base_description=DEFAULT_DESCRIPTION
    )
    assert evidence["appended_chars"] == len(rendered) - len(DEFAULT_DESCRIPTION)
    assert evidence["authority"] == "disclosure_only"
    assert evidence["profiles"] == ["research_worker"]
    assert evidence["digest"] != evidence["base_digest"]


def test_card_never_names_the_delegation_surface_as_available() -> None:
    for profile in (RESEARCH, CODING):
        built = card(profile)
        assert "delegate_tasks" not in built.allowed
        assert "delegate.read" not in built.allowed
        assert "delegate_tasks" in built.not_granted


def test_reference_surface_matches_the_run_vocabulary() -> None:
    """The "does not have" list is only meaningful if the reference set is the real surface."""

    assert {
        "bash",
        "file_read",
        "file_write",
        "file_edit",
        "grep",
        "list_files",
        "tool.search",
        "dispatch_action",
        "run_public_tests",
        "delegate_tasks",
        "delegate.read",
    } == set(REFERENCE_SURFACE)
