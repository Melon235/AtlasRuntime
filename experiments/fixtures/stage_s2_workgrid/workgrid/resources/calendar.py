"""Availability windows.

Time is a line of integer slots. A window ``[start, end)`` is open; a slot outside every window is
closed. A task of duration ``d`` therefore needs ``d`` *consecutive* open slots, and because windows
are disjoint a run never spans two of them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from workgrid.resources.errors import InvalidWindowError


@dataclass(frozen=True)
class AvailabilityWindow:
    """Half-open slot range: ``start`` is open, ``end`` is not."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or not isinstance(self.end, int):
            raise InvalidWindowError(
                "window bounds must be integers", start=self.start, end=self.end
            )
        if self.start < 0:
            raise InvalidWindowError("window cannot start before slot 0", start=self.start)
        if self.end <= self.start:
            raise InvalidWindowError(
                "window end must be after its start", start=self.start, end=self.end
            )

    def __len__(self) -> int:
        return self.end - self.start

    def contains(self, slot: int) -> bool:
        return self.start <= slot < self.end

    def as_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass(frozen=True)
class Calendar:
    """Every open window, sorted and disjoint."""

    windows: tuple[AvailabilityWindow, ...] = ()

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.windows, key=lambda window: (window.start, window.end)))
        for previous, current in zip(ordered, ordered[1:], strict=False):
            if current.start < previous.end:
                raise InvalidWindowError(
                    "windows must not overlap",
                    previous=previous.as_dict(),
                    current=current.as_dict(),
                )
        object.__setattr__(self, "windows", ordered)

    def is_open(self, slot: int) -> bool:
        return any(window.contains(slot) for window in self.windows)

    def next_open(self, slot: int) -> int | None:
        """The first open slot at or after ``slot``, or ``None`` past the last window."""

        for window in self.windows:
            if window.contains(slot):
                return slot
            if window.start > slot:
                return window.start
        return None

    def earliest_start(self, not_before: int, duration: int) -> int | None:
        """Earliest start of ``duration`` consecutive open slots at or after ``not_before``."""

        if duration <= 0:
            return not_before
        for window in self.windows:
            candidate = max(not_before, window.start)
            if candidate + duration <= window.end:
                return candidate
        return None

    def fits(self, start: int, duration: int) -> bool:
        """True when every slot of ``[start, start + duration)`` is open."""

        if duration <= 0:
            return True
        return any(
            window.start <= start and start + duration <= window.end for window in self.windows
        )

    def span(self) -> tuple[int, int]:
        if not self.windows:
            return (0, 0)
        return (self.windows[0].start, self.windows[-1].end)

    def usable_slots(self) -> int:
        return sum(len(window) for window in self.windows)

    def as_list(self) -> list[dict[str, int]]:
        return [window.as_dict() for window in self.windows]


def build_calendar(spec: Iterable[Mapping[str, object]]) -> Calendar:
    """Read the ``windows`` list of a plan document."""

    windows: list[AvailabilityWindow] = []
    for index, raw in enumerate(spec):
        if not isinstance(raw, Mapping):
            raise InvalidWindowError("each window must be a mapping", index=index)
        try:
            start = raw["start"]
            end = raw["end"]
        except KeyError as exc:
            raise InvalidWindowError(
                "a window needs 'start' and 'end'", index=index, missing=str(exc)
            ) from exc
        windows.append(AvailabilityWindow(start=start, end=end))  # type: ignore[arg-type]
    return Calendar(tuple(windows))
