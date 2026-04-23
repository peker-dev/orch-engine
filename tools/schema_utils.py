"""Shared JSON schema utilities used by both adapters and the probe tool.

Kept intentionally small: covers the subset of JSON Schema the engine relies
on (object/array/string/integer/number/boolean plus enum/const/min/max,
minItems, additionalProperties=false). Extend here — not in callers — when a
new schema feature is needed so adapters and probes stay in lockstep.
"""

from __future__ import annotations

import json
from typing import Any, Iterator


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


def _find_balanced_end(text: str, start: int) -> int | None:
    """Return the index of the matching closer for the bracket at `start`.

    Respects JSON string literals and backslash escapes so that `{`/`}` /
    `[`/`]` inside quoted strings do not disturb depth tracking.
    """
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return None


def _scan_json_blocks(text: str) -> Iterator[str]:
    """Yield substrings that look like balanced JSON objects/arrays.

    LLMs often prepend conversational prose to the required JSON envelope
    (e.g. "Done! Here is the result:\n\n{...}"). This scanner locates every
    balanced `{...}` / `[...]` block inside `text` so callers can try each
    candidate in order.
    """
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{" or c == "[":
            end = _find_balanced_end(text, i)
            if end is not None:
                yield text[i:end + 1]
                i = end + 1
                continue
        i += 1


def _string_candidates_for_scan(value: str) -> Iterator[Any]:
    """Parse embedded JSON blocks out of a prose-wrapped string.

    Fast path: whole string is JSON → yield once. Otherwise scan for
    balanced JSON blocks and yield each successfully parsed value. Used by
    both candidate finders so prose-prefixed LLM envelopes are recoverable.
    """
    text = value.strip()
    if not text:
        return
    if text.startswith("{") or text.startswith("["):
        try:
            yield json.loads(text)
            return
        except json.JSONDecodeError:
            pass
    for block in _scan_json_blocks(text):
        try:
            yield json.loads(block)
        except json.JSONDecodeError:
            continue


def find_payload_candidate(value: Any, required: set[str]) -> dict[str, Any] | None:
    """Recursively look for a dict that has every key in `required`.

    Strings that look like JSON are parsed and re-checked. When the string
    contains a JSON envelope preceded by conversational prose (a common LLM
    habit), embedded balanced JSON blocks are scanned as additional
    candidates. This is necessary because Claude CLI occasionally packs
    the role JSON inside a wrapper string field such as `result`.
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
        for parsed in _string_candidates_for_scan(value):
            found = find_payload_candidate(parsed, required)
            if found is not None:
                return found
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
        for parsed in _string_candidates_for_scan(value):
            found = find_first_dict_candidate(parsed)
            if found is not None:
                return found
        return None

    return None
