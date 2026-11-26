"""Public checks for the v2 configuration lifecycle."""

import pytest

from configkit.document import parse
from configkit.errors import (
    ConstraintViolation,
    MigrationError,
    ProfileError,
    TypeMismatch,
    UnknownKey,
)
from configkit.explain import as_env, diff, render
from configkit.loader import load, read_env
from configkit.migrate import upgrade
from configkit.profile import load as load_profile
from configkit.profile import names as profile_names
from configkit.runtime import Settings
from configkit.validate import check


def test_the_document_flattens_nested_tables():
    text = """
[retry]
max = 4

[retry.backoff]
delay = 1.5

[server]
host = "api"
"""
    assert parse(text) == {
        "retry.max": 4,
        "retry.backoff.delay": 1.5,
        "server.host": "api",
    }


def test_the_environment_prefix_maps_to_dotted_paths():
    env = {"CONFIGKIT__RETRY__MAX": "7", "CONFIGKIT__HOST": "env-host", "OTHER": "ignored"}
    assert read_env(env) == {"retry.max": "7", "host": "env-host"}
    assert load("[retry]\nmax = 1\n", env) == {"retry.max": "7", "host": "env-host"}


def test_precedence_is_explicit_env_file_default():
    settings = Settings.from_sources(
        file_text='host = "file"\nport = 9000\n[retry]\nmax = 2\n',
        env_values={"CONFIGKIT__HOST": "env", "CONFIGKIT__PORT": "8000"},
        explicit={"retry.max": 5},
    )
    assert settings["host"] == "env"
    assert settings["port"] == 8000
    assert settings["retry.max"] == 5
    assert settings["timeout"] == 30.0
    assert settings.explain("host") == "env"
    assert settings.explain("retry.max") == "explicit"
    assert settings.explain("timeout") == "default"


def test_migration_renames_and_drops_legacy_keys():
    upgraded = upgrade(
        {
            "max_retries": 2,
            "retry_delay": 1.5,
            "tls_cert": "cert.pem",
            "tls_key": "key.pem",
            "legacy_flag": True,
            "host": "h",
        }
    )
    assert upgraded == {
        "retry.max": 2,
        "retry.delay": 1.5,
        "tls.cert": "cert.pem",
        "tls.key": "key.pem",
        "host": "h",
    }


def test_validation_is_strict_and_applies_defaults():
    assert check({"port": "8080"})["port"] == 8080
    assert check({})["features"] == []
    with pytest.raises(TypeMismatch):
        check({"port": True})
    with pytest.raises(TypeMismatch):
        check({"port": "12x"})
    with pytest.raises(UnknownKey):
        check({"nope": 1})


def test_cross_field_rules_are_enforced():
    with pytest.raises(ConstraintViolation):
        check({"tls.enabled": True})
    checked = check({"tls.enabled": True, "tls.cert": "c", "tls.key": "k"})
    assert checked["tls.enabled"] is True
    with pytest.raises(ConstraintViolation):
        check({"retry.delay": 10.0, "retry.max": 5, "timeout": 30.0})


def test_environment_values_are_coerced():
    settings = Settings.from_sources(
        env_values={
            "CONFIGKIT__RETRY__JITTER": "TRUE",
            "CONFIGKIT__FEATURES": "audit, metrics",
        }
    )
    assert settings["retry.jitter"] is True
    assert settings["features"] == ["audit", "metrics"]
    assert settings.explain("retry.jitter") == "env"


def test_the_version_metadata_is_validated():
    with pytest.raises(MigrationError):
        upgrade({"version": 3})
    with pytest.raises(MigrationError):
        upgrade({"version": True})


def test_a_profile_sits_between_the_file_and_the_environment():
    from_file = Settings.from_sources(file_text='log.level = "error"\n', profile="dev")
    assert from_file["log.level"] == "debug"
    assert from_file.explain("log.level") == "profile"
    overridden = Settings.from_sources(
        profile="dev",
        env_values={"CONFIGKIT__LOG__LEVEL": "warning"},
    )
    assert overridden["log.level"] == "warning"
    assert overridden.explain("log.level") == "env"
    assert profile_names() == ["dev", "prod", "staging"]
    assert load_profile("dev")["database.pool"] == 2
    with pytest.raises(ProfileError):
        load_profile("nope")


def test_the_cache_flag_and_the_feature_list_must_agree():
    with pytest.raises(ConstraintViolation):
        check({"cache.enabled": True})
    checked = check({"cache.enabled": True, "features": ["cache"]})
    assert checked["cache.enabled"] is True
    assert checked["cache.namespace"] == "app"


def test_the_explanation_renders_and_round_trips():
    settings = Settings.from_sources(profile="staging")
    text = render(settings)
    assert "log.level=info [profile]" in text
    assert text.splitlines() == sorted(text.splitlines())
    environment = as_env(settings)
    assert environment["CONFIGKIT__LOG__LEVEL"] == "info"
    assert environment["CONFIGKIT__FEATURES"] == "cache,metrics"
    again = Settings.from_sources(env_values=environment)
    assert again.as_dict() == settings.as_dict()
    changes = diff(settings, Settings.from_sources(profile="dev"))
    assert changes["log.level"] == ("info", "debug")
    assert "database.url" in changes


def test_explicit_nested_tables_are_flattened():
    settings = Settings.from_sources(explicit={"retry": {"max": 6}, "tls": {"enabled": False}})
    assert settings["retry.max"] == 6
    assert settings.explain("retry.max") == "explicit"
    assert settings.as_dict() == dict(sorted(settings.as_dict().items()))
