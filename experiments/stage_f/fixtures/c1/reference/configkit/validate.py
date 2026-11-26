"""Validation (v2): strict coercion, ranges, choices and cross-field rules."""

from __future__ import annotations

import math

from configkit.document import flatten
from configkit.errors import ConstraintViolation, TypeMismatch, UnknownKey
from configkit.spec import FEATURE_FLAGS, FIELDS, MISSING, FieldSpec


def _text(spec: FieldSpec, value: object) -> str:
    if not isinstance(value, str):
        raise TypeMismatch(spec.name, f"expected a string, got {type(value).__name__}")
    text = value.strip()
    if spec.non_empty and not text:
        raise TypeMismatch(spec.name, "must not be empty")
    if spec.choices and text not in spec.choices:
        raise ConstraintViolation(spec.name, f"{text!r} is not one of {list(spec.choices)}")
    return text


def _number(spec: FieldSpec, value: object) -> float:
    if isinstance(value, bool):
        raise TypeMismatch(spec.name, "bool is not a number")
    if spec.kind == "int" and isinstance(value, float):
        raise TypeMismatch(spec.name, f"{value!r} is not an integer")
    if isinstance(value, (int, float)):
        number = float(value) if spec.kind == "float" else value
    elif isinstance(value, str):
        text = value.strip()
        if spec.kind == "int":
            body = text[1:] if text.startswith("-") else text
            if not body.isdigit():
                raise TypeMismatch(spec.name, f"{value!r} is not an integer")
            number = int(text)
        else:
            try:
                number = float(text)
            except ValueError as error:
                raise TypeMismatch(spec.name, f"{value!r} is not a number") from error
    else:
        raise TypeMismatch(spec.name, f"expected a number, got {type(value).__name__}")
    if isinstance(number, float) and not math.isfinite(number):
        raise TypeMismatch(spec.name, "must be finite")
    if spec.minimum is not None and number < spec.minimum:
        raise ConstraintViolation(spec.name, f"{number} is below {spec.minimum}")
    if spec.maximum is not None and number > spec.maximum:
        raise ConstraintViolation(spec.name, f"{number} is above {spec.maximum}")
    return number


def _boolean(spec: FieldSpec, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise TypeMismatch(spec.name, f"{value!r} is not a boolean")


def _sequence(spec: FieldSpec, value: object) -> list[str]:
    if isinstance(value, str):
        raw = value.split(",") if value.strip() else []
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raise TypeMismatch(spec.name, f"{value!r} is not a list")
    items: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise TypeMismatch(spec.name, "list items must be strings")
        text = item.strip()
        if text:
            items.append(text)
    if spec.name == "features":
        unknown = sorted({item for item in items if item not in FEATURE_FLAGS})
        if unknown:
            raise ConstraintViolation(spec.name, f"unknown feature flags: {unknown}")
    return items


_COERCERS = {
    "str": _text,
    "int": _number,
    "float": _number,
    "bool": _boolean,
    "list": _sequence,
}


def _coerce(spec: FieldSpec, value: object):
    coercer = _COERCERS.get(spec.kind)
    if coercer is None:
        raise TypeMismatch(spec.name, f"unsupported kind {spec.kind!r}")
    return coercer(spec, value)


def check(values: dict) -> dict:
    flat = flatten(values)
    unknown = sorted(set(flat) - set(FIELDS))
    if unknown:
        raise UnknownKey(unknown[0], "key is not part of the frozen spec")

    checked: dict = {}
    for name, spec in FIELDS.items():
        if name in flat:
            checked[name] = _coerce(spec, flat[name])
        elif spec.default is not MISSING:
            checked[name] = _coerce(spec, spec.default)

    if checked["tls.enabled"] and (not checked["tls.cert"] or not checked["tls.key"]):
        raise ConstraintViolation("tls.cert", "tls.enabled requires tls.cert and tls.key")
    if checked["cache.enabled"] and "cache" not in checked["features"]:
        raise ConstraintViolation("cache.enabled", "the cache feature flag must be enabled too")
    budget = float(checked["retry.delay"]) * float(checked["retry.max"])
    if budget > float(checked["timeout"]):
        raise ConstraintViolation(
            "retry.delay",
            f"retry.delay * retry.max ({budget}) exceeds timeout ({checked['timeout']})",
        )
    return {name: checked[name] for name in FIELDS}
