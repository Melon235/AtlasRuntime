"""CSV cell helpers (v1).

v1 splits on every comma and lower-cases the header, which is enough for the happy path only.
"""

from __future__ import annotations


def split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.split(",")]


def header_index(cells: list[str]) -> dict[str, int]:
    return {cell.strip().lower(): index for index, cell in enumerate(cells)}
