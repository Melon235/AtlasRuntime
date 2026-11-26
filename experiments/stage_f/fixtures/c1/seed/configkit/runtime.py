"""Runtime view (v1): a dict subclass with no provenance."""

from __future__ import annotations

from configkit.document import parse
from configkit.merge import resolve
from configkit.spec import DEFAULTS
from configkit.validate import check


class Settings(dict):
    @classmethod
    def from_sources(cls, *, file_text: str = "", env_values=None, explicit=None) -> Settings:
        values = check(
            resolve(DEFAULTS, parse(file_text), dict(env_values or {}), dict(explicit or {}))
        )
        return cls(values)

    def as_dict(self) -> dict:
        return dict(self)

    def explain(self, key: str) -> str:
        return "default"
