"""Search indexing (already migrated — leave the behaviour alone)."""

from __future__ import annotations

from foundation.audit import record


def index(term: str) -> object:
    return record(kind="search", data={"term": term})
