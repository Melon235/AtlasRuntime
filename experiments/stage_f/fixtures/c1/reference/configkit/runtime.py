"""Runtime view (v2): an immutable, validated mapping with provenance."""

from __future__ import annotations

from dataclasses import dataclass, field

from configkit import document, loader, merge, migrate, validate
from configkit import profile as profiles
from configkit.spec import DEFAULTS, FIELDS


@dataclass(frozen=True)
class Settings:
    values: dict = field(default_factory=dict)
    origins: dict = field(default_factory=dict)

    @classmethod
    def from_sources(
        cls,
        *,
        file_text: str = "",
        env_values=None,
        explicit=None,
        profile: str | None = None,
    ) -> Settings:
        file_values = document.parse(file_text)
        env_layer = loader.read_env(dict(env_values or {}))
        explicit_layer = document.flatten(dict(explicit or {}))
        profile_layer = profiles.load(profile) if profile is not None else {}
        migrated = migrate.upgrade(file_values)
        resolved = merge.resolve(
            DEFAULTS,
            migrated,
            env_layer,
            explicit_layer,
            profile_layer,
        )
        checked = validate.check(resolved.values)
        return cls(values=checked, origins=resolved.origins)

    @classmethod
    def from_profile(cls, name: str, **sources) -> Settings:
        return cls.from_sources(profile=name, **sources)

    def __getitem__(self, key: str):
        return self.values[key]

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def __len__(self) -> int:
        return len(self.values)

    def get(self, key: str, default=None):
        if key not in FIELDS:
            return default
        return self.values.get(key, default)

    def as_dict(self) -> dict:
        return dict(sorted(self.values.items()))

    def explain(self, key: str) -> str:
        if key not in self.origins:
            raise KeyError(key)
        return self.origins[key]


__all__ = ["Settings"]
