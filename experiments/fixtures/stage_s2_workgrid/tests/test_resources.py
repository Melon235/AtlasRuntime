"""Compatibility suite for the resource package.

These tests describe behaviour that callers already rely on. They must keep passing unchanged.
"""

from __future__ import annotations

import pytest
from workgrid.resources import (
    AvailabilityWindow,
    Calendar,
    DemandExceedsCapacityError,
    InvalidDemandError,
    InvalidPoolError,
    InvalidWindowError,
    PoolSet,
    ResourceConstraints,
    ResourcePool,
    TaskDemand,
    UnknownResourceError,
    build_calendar,
    build_demand,
    build_pools,
)

POOLS = PoolSet((ResourcePool("workers", 2), ResourcePool("gpu", 1)))


def calendar() -> Calendar:
    return Calendar((AvailabilityWindow(0, 4), AvailabilityWindow(6, 10)))


# -- pools -------------------------------------------------------------------


def test_a_pool_needs_a_name_and_a_non_negative_capacity() -> None:
    with pytest.raises(InvalidPoolError):
        ResourcePool("", 1)
    with pytest.raises(InvalidPoolError):
        ResourcePool("workers", -1)


def test_a_boolean_capacity_is_not_an_integer() -> None:
    with pytest.raises(InvalidPoolError):
        ResourcePool("workers", True)  # type: ignore[arg-type]


def test_duplicate_pool_names_are_rejected() -> None:
    with pytest.raises(InvalidPoolError):
        PoolSet((ResourcePool("workers", 1), ResourcePool("workers", 2)))


def test_pools_are_ordered_by_name() -> None:
    pools = PoolSet((ResourcePool("gpu", 1), ResourcePool("workers", 2)))
    assert pools.names() == ("gpu", "workers")
    assert pools.capacity("workers") == 2
    assert pools.total() == 3


def test_an_unknown_pool_raises() -> None:
    with pytest.raises(UnknownResourceError):
        POOLS.capacity("memory")


# -- demand ------------------------------------------------------------------


def test_negative_units_are_rejected() -> None:
    with pytest.raises(InvalidDemandError):
        TaskDemand("a", {"workers": -1})


def test_units_are_sorted_and_default_to_zero() -> None:
    demand = TaskDemand("a", {"workers": 1, "gpu": 1})
    assert demand.resources() == ("gpu", "workers")
    assert demand.units_for("workers") == 1
    assert demand.units_for("memory") == 0


def test_demand_against_an_unknown_resource_raises() -> None:
    with pytest.raises(UnknownResourceError):
        TaskDemand("a", {"memory": 1}).validate_against(POOLS)


def test_demand_above_capacity_raises() -> None:
    with pytest.raises(DemandExceedsCapacityError) as error:
        TaskDemand("a", {"workers": 3}).validate_against(POOLS)
    assert error.value.as_dict() == {
        "code": "demand_exceeds_capacity",
        "message": "task 'a' needs 3 of 'workers', pool capacity is 2",
        "task": "a",
        "resource": "workers",
        "demand": 3,
        "capacity": 2,
    }


def test_demand_equal_to_capacity_is_allowed() -> None:
    TaskDemand("a", {"workers": 2}).validate_against(POOLS)


def test_demand_document_is_read() -> None:
    assert build_demand("a", None).units == {}
    assert build_demand("a", {"workers": 1}).units == {"workers": 1}


# -- calendar ----------------------------------------------------------------


def test_a_window_must_end_after_it_starts() -> None:
    with pytest.raises(InvalidWindowError):
        AvailabilityWindow(4, 4)
    with pytest.raises(InvalidWindowError):
        AvailabilityWindow(-1, 2)


def test_windows_must_not_overlap() -> None:
    with pytest.raises(InvalidWindowError):
        Calendar((AvailabilityWindow(0, 4), AvailabilityWindow(3, 6)))


def test_windows_are_sorted() -> None:
    ordered = Calendar((AvailabilityWindow(6, 10), AvailabilityWindow(0, 4)))
    assert ordered.as_list() == [{"start": 0, "end": 4}, {"start": 6, "end": 10}]


def test_openness_of_a_slot() -> None:
    cal = calendar()
    assert cal.is_open(0) is True
    assert cal.is_open(4) is False
    assert cal.is_open(5) is False
    assert cal.is_open(9) is True
    assert cal.is_open(10) is False


def test_next_open_slot() -> None:
    cal = calendar()
    assert cal.next_open(0) == 0
    assert cal.next_open(4) == 6
    assert cal.next_open(7) == 7
    assert cal.next_open(10) is None


def test_earliest_start_needs_consecutive_slots() -> None:
    cal = calendar()
    assert cal.earliest_start(0, 2) == 0
    assert cal.earliest_start(3, 2) == 6
    assert cal.earliest_start(9, 2) is None
    assert cal.earliest_start(0, 5) is None


def test_fits_never_spans_a_gap() -> None:
    cal = calendar()
    assert cal.fits(0, 4) is True
    assert cal.fits(2, 4) is False
    assert cal.fits(6, 4) is True


def test_span_and_usable_slots() -> None:
    assert calendar().span() == (0, 10)
    assert calendar().usable_slots() == 8


def test_a_calendar_document_is_read() -> None:
    assert build_calendar([{"start": 0, "end": 2}]).usable_slots() == 2


def test_a_pool_document_is_read() -> None:
    assert build_pools([{"name": "workers", "capacity": 2}]).capacity("workers") == 2


# -- constraints -------------------------------------------------------------


def constraints() -> ResourceConstraints:
    return ResourceConstraints(pools=POOLS, calendar=calendar())


def test_usage_at_counts_overlapping_placements() -> None:
    placed = [(0, 2, {"workers": 1}), (1, 3, {"workers": 1})]
    assert constraints().usage_at(0, placed) == {"gpu": 0, "workers": 1}
    assert constraints().usage_at(1, placed) == {"gpu": 0, "workers": 2}
    assert constraints().usage_at(3, placed) == {"gpu": 0, "workers": 0}


def test_peak_usage() -> None:
    placed = [(0, 2, {"workers": 1}), (1, 3, {"workers": 1}), (2, 3, {"gpu": 1})]
    assert constraints().peak_usage(placed) == {"gpu": 1, "workers": 2}


def test_demand_is_refused_when_capacity_is_full() -> None:
    subject = constraints()
    placed = [(0, 4, {"workers": 2})]
    assert subject.fits_at(0, 1, TaskDemand("x", {"workers": 1}), placed) is False
    assert subject.fits_at(0, 1, TaskDemand("x", {}), placed) is True


def test_a_closed_slot_never_fits() -> None:
    subject = constraints()
    assert subject.fits_at(4, 1, TaskDemand("x", {}), ()) is False
    assert subject.fits_at(6, 1, TaskDemand("x", {}), ()) is True


def test_a_placement_longer_than_the_window_never_fits() -> None:
    subject = constraints()
    assert subject.fits_at(0, 5, TaskDemand("x", {}), ()) is False
