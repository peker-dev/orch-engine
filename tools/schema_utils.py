"""Shared JSON schema utilities used by both adapters and the probe tool.

Kept intentionally small: covers the subset of JSON Schema the engine relies
on (object/array/string/integer/number/boolean plus enum/const/min/max,
minItems, additionalProperties=false). Extend here — not in callers — when a
new schema feature is needed so adapters and probes stay in lockstep.
"""

from __future__ import annotations

import json
from typing import Any


def validate_schema(instance: Any, schema: dict[str, Any], path: str = "root") -> None:
    schema_type = schema.get("type")

    if schema_type == "object":
        if not isinstance(instance, dict):
            raise ValueError(f"{path}: expected object")
        required = schema.get("required", [])
        for key in required:
            if key not in instance:
                raise ValueError(f"{path}: missing required key '{key}'")
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {}).keys())
            extras = set(instance.keys()) - allowed
            if extras:
                raise ValueError(f"{path}: unexpected keys {sorted(extras)}")
        for key, subschema in schema.get("properties", {}).items():
            if key in instance:
                validate_schema(instance[key], subschema, f"{path}.{key}")
        return

    if schema_type == "array":
        if not isinstance(instance, list):
            raise ValueError(f"{path}: expected array")
        min_items = schema.get("minItems")
        if min_items is not None and len(instance) < min_items:
            raise ValueError(f"{path}: expected at least {min_items} items")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(instance):
                validate_schema(item, item_schema, f"{path}[{index}]")
        return

    if schema_type == "string":
        if not isinstance(instance, str):
            raise ValueError(f"{path}: expected string")
    elif schema_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            raise ValueError(f"{path}: expected integer")
    elif schema_type == "number":
        if not isinstance(instance, (int, float)) or isinstance(instance, bool):
            raise ValueError(f"{path}: expected number")
    elif schema_type == "boolean":
        if not isinstance(instance, bool):
            raise ValueError(f"{path}: expected boolean")

    if "enum" in schema and instance not in schema["enum"]:
        raise ValueError(f"{path}: value {instance!r} not in enum")

    if "const" in schema and instance != schema["const"]:
        raise ValueError(f"{path}: value {instance!r} does not match const")

    if "minimum" in schema and instance < schema["minimum"]:
        raise ValueError(f"{path}: value {instance!r} below minimum {schema['minimum']}")

    if "maximum" in schema and instance > schema["maximum"]:
        raise ValueError(f"{path}: value {instance!r} above maximum {schema['maximum']}")


def find_payload_candidate(value: Any, required: set[str]) -> dict[str, Any] | None:
    """Recursively look for a dict that has every key in `required`.

    Strings that look like JSON are parsed and re-checked. This is necessary
    because Claude CLI occasionally packs the role JSON inside a wrapper
    string field such as `result`.
    """
    if isinstance(value, dict):
        if required.issubset(set(value.keys())):
            return value
        for nested in value.values():
            found = find_payload_candidate(nested, required)
            if found is not None:
                return found
        return None

    if isinstance(value, list):
        for item in value:
            found = find_payload_candidate(item, required)
            if found is not None:
                return found
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return find_payload_candidate(parsed, required)
        return None

    return None


def find_first_dict_candidate(value: Any) -> dict[str, Any] | None:
    """Return the first dict found while walking the structure.

    Used as a last-resort fallback when we have an object that looks like a
    role payload but is missing one or two required keys — normalize layers
    can fill the gaps afterward.
    """
    if isinstance(value, dict):
        return value

    if isinstance(value, list):
        for item in value:
            found = find_first_dict_candidate(item)
            if found is not None:
                return found
        return None

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return None
            return find_first_dict_candidate(parsed)
        return None

    return None
