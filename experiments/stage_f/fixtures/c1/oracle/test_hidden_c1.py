"""Hidden checks for the configuration lifecycle (never staged into the agent workspace)."""

import pytest

from configkit.document import parse
from configkit.errors import (
    ConfigError,
    ConstraintViolation,
    MigrationError,
    ParseError,
    ProfileError,
    TypeMismatch,
    UnknownKey,
)
from configkit.explain import as_env, diff, render
from configkit.loader import load, read_env
from configkit.merge import resolve
from configkit.migrate import upgrade
from configkit.profile import PROFILES, load as load_profile, names as profile_names
from configkit.runtime import Settings
from configkit.spec import DEFAULTS, FIELDS
from configkit.validate import check


def test_errors_carry_a_code_and_a_path():
    with pytest.raises(ConfigError) as unknown:
        check({"nope": 1})
    assert (unknown.value.code, unknown.value.path) == ("unknown_key", "nope")
    with pytest.raises(ConfigError) as mismatch:
        check({"port": "x"})
    assert (mismatch.value.code, mismatch.value.path) == ("type_mismatch", "port")
    with pytest.raises(ConfigError) as constraint:
        check({"log.level": "verbose"})
    assert (constraint.value.code, constraint.value.path) == ("constraint", "log.level")
    with pytest.raises(ConfigError) as migration:
        upgrade({"version": 9})
    assert migration.value.code == "migration"
    with pytest.raises(ConfigError) as parsed:
        parse("not toml at all")
    assert parsed.value.code == "parse"


def test_document_rejects_invalid_toml_and_arrays_of_tables():
    assert parse("") == {}
    assert parse("a = [1, 2]") == {"a": [1, 2]}
    with pytest.raises(ParseError):
        parse("[[a]]\nx = 1\n")
    with pytest.raises(ParseError):
        parse("a = \n")


def test_environment_mapping_is_exact_and_validated():
    assert read_env({"OTHER": "1", "ConfigKit__HOST": "x"}) == {}
    with pytest.raises(ParseError):
        read_env({"CONFIGKIT__": "x"})
    with pytest.raises(ParseError):
        read_env({"CONFIGKIT__RETRY____MAX": "1"})
    with pytest.raises(ParseError):
        read_env({"CONFIGKIT__HOST": 1})
    assert load('host = "file"\n', {"CONFIGKIT__HOST": "env"}) == {"host": "env"}


def test_migration_is_idempotent_and_version_aware():
    v1 = {"version": 1, "max_retries": 2, "legacy_flag": True, "host": "h"}
    once = upgrade(v1)
    assert once == {"retry.max": 2, "host": "h"}
    assert upgrade(once) == once
    assert upgrade({"version": 2, "host": "h"}) == {"host": "h"}
    assert upgrade({"max_retries": 2, "retry.max": 9}) == {"retry.max": 9}
    assert upgrade({"retry_delay": 1.0, "tls_cert": "c", "tls_key": "k"}) == {
        "retry.delay": 1.0,
        "tls.cert": "c",
        "tls.key": "k",
    }


def test_merge_tracks_every_layer_and_carries_unknown_keys():
    resolved = resolve(
        {"host": "default", "port": 1},
        {"host": "file", "retry.max": 2},
        {"port": "3"},
        {"host": "explicit", "extra": True},
    )
    assert resolved.values == {"host": "explicit", "port": "3", "retry.max": 2, "extra": True}
    assert resolved.origins == {
        "host": "explicit",
        "port": "env",
        "retry.max": "file",
        "extra": "explicit",
    }
    assert resolve({}, {}, {}, {}).values == {}


def test_the_file_never_beats_the_environment():
    resolved = resolve({"host": "default"}, {"host": "file"}, {"host": "env"}, {})
    assert resolved.values["host"] == "env"
    assert resolved.origins["host"] == "env"
    settings = Settings.from_sources(
        file_text='host = "file"\n',
        env_values={"CONFIGKIT__HOST": "env"},
    )
    assert settings["host"] == "env"


def test_numeric_coercion_is_strict():
    with pytest.raises(ConstraintViolation):
        check({"port": "-1"})
    with pytest.raises(TypeMismatch):
        check({"port": 1.0})
    with pytest.raises(TypeMismatch):
        check({"timeout": float("nan")})
    with pytest.raises(TypeMismatch):
        check({"timeout": float("inf")})
    with pytest.raises(TypeMismatch):
        check({"retry.max": "2.5"})
    with pytest.raises(ConstraintViolation):
        check({"port": 70000})
    assert check({"timeout": "12.5"})["timeout"] == 12.5
    assert check({"port": "0008"})["port"] == 8


def test_boolean_and_list_coercion():
    for value, expected in (("true", True), ("FALSE", False), (True, True), (False, False)):
        assert check({"retry.jitter": value})["retry.jitter"] is expected
    with pytest.raises(TypeMismatch):
        check({"retry.jitter": "yes"})
    assert check({"features": "audit, , metrics"})["features"] == ["audit", "metrics"]
    assert check({"features": ["cache", "tracing"]})["features"] == ["cache", "tracing"]
    assert check({"features": ""})["features"] == []
    with pytest.raises(TypeMismatch):
        check({"features": [1]})
    with pytest.raises(ConstraintViolation):
        check({"features": ["nope"]})


def test_defaults_fill_every_field_and_ranges_hold():
    checked = check({})
    assert set(checked) == set(FIELDS)
    assert checked["host"] == "localhost"
    assert checked["port"] == 8080
    assert checked["timeout"] == 30.0
    assert checked["retry.max"] == 3
    assert checked["retry.delay"] == 0.5
    assert checked["retry.jitter"] is False
    assert checked["tls.enabled"] is False
    assert checked["log.level"] == "info"
    assert checked["features"] == []
    assert check({"retry.delay": 10.0, "retry.max": 3, "timeout": 30.0})["retry.delay"] == 10.0


def test_cross_field_rules_and_paths():
    with pytest.raises(ConstraintViolation) as error:
        check({"tls.enabled": True, "tls.cert": "c"})
    assert error.value.path == "tls.cert"
    with pytest.raises(ConstraintViolation):
        check({"retry.delay": 4.0, "retry.max": 10, "timeout": 30.0})
    assert check({"retry.delay": 3.0, "retry.max": 10, "timeout": 30.0})["timeout"] == 30.0


def test_runtime_view_is_complete_sorted_and_explainable():
    settings = Settings.from_sources(
        file_text='version = 1\nmax_retries = 4\nhost = "file"\n',
        env_values={"CONFIGKIT__PORT": "9000"},
        explicit={"log.level": "debug"},
    )
    assert settings["retry.max"] == 4
    assert settings.explain("retry.max") == "file"
    assert settings.explain("port") == "env"
    assert settings.explain("log.level") == "explicit"
    assert settings.explain("timeout") == "default"
    assert settings.as_dict() == dict(sorted(settings.as_dict().items()))
    assert set(settings.as_dict()) == set(FIELDS)
    assert settings.get("nope", "fallback") == "fallback"
    with pytest.raises(KeyError):
        settings.explain("nope")
    with pytest.raises(UnknownKey):
        Settings.from_sources(explicit={"retry": {"nope": 1}})


def test_profiles_are_a_layer_with_their_own_precedence():
    assert profile_names() == sorted(PROFILES)
    assert set(profile_names()) == {"dev", "staging", "prod"}
    copy = load_profile("prod")
    copy["log.level"] = "debug"
    assert PROFILES["prod"]["log.level"] == "warning"
    with pytest.raises(ProfileError) as error:
        load_profile("nope")
    assert (error.value.code, error.value.path) == ("profile", "nope")

    layered = Settings.from_sources(
        file_text='log.level = "error"\ndatabase.pool = 3\n',
        profile="prod",
        env_values={"CONFIGKIT__DATABASE__POOL": "7"},
        explicit={"cache.namespace": "explicit"},
    )
    assert layered["log.level"] == "warning"
    assert layered.explain("log.level") == "profile"
    assert layered["database.pool"] == 7
    assert layered.explain("database.pool") == "env"
    assert layered["cache.namespace"] == "explicit"
    assert layered["timeout"] == 60.0
    assert layered.explain("timeout") == "profile"


def test_new_fields_are_validated_like_the_rest():
    with pytest.raises(ConstraintViolation):
        check({"database.pool": 51})
    with pytest.raises(ConstraintViolation):
        check({"cache.ttl": 0.5})
    with pytest.raises(ConstraintViolation):
        check({"logging.format": "xml"})
    with pytest.raises(TypeMismatch):
        check({"database.url": ""})
    assert check({"logging.format": "json"})["logging.format"] == "json"
    assert check({})["database.url"] == "sqlite:///app.db"


def test_explanation_output_is_stable_and_round_trips():
    settings = Settings.from_sources(profile="staging")
    lines = render(settings).splitlines()
    assert lines == sorted(lines)
    assert all(" [" in line and line.endswith("]") for line in lines)
    environment = as_env(settings)
    assert environment == dict(sorted(environment.items()))
    again = Settings.from_sources(env_values=environment)
    assert again.as_dict() == settings.as_dict()
    assert diff(settings, again) == {}
    changes = diff(settings, Settings.from_sources(profile="prod"))
    assert changes["log.level"] == ("info", "warning")
    assert "cache.ttl" in changes


def test_the_defaults_table_is_the_one_the_runtime_uses():
    settings = Settings.from_sources()
    assert settings.as_dict() == {
        name: check({})[name] for name in FIELDS
    }
    assert settings["port"] == DEFAULTS["port"]
