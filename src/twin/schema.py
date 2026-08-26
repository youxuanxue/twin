from __future__ import annotations

import json
import re
from pathlib import Path

from twin.resources import ResourceCatalog


def validate_document(
    value: object, schema_name: str, resources: ResourceCatalog
) -> list[str]:
    schema = json.loads(resources.schema(schema_name).read_text(encoding="utf-8"))
    errors: list[str] = []
    _check(value, schema, "$", errors)
    return errors


def _matches_type(value: object, expected: str) -> bool:
    types = {
        "object": lambda: isinstance(value, dict),
        "array": lambda: isinstance(value, list),
        "string": lambda: isinstance(value, str),
        "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
        "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": lambda: isinstance(value, bool),
        "null": lambda: value is None,
    }
    return types.get(expected, lambda: True)()


def _check(value: object, schema: dict[str, object], path: str, errors: list[str]) -> None:
    expected = schema.get("type")
    if isinstance(expected, list):
        if not any(isinstance(item, str) and _matches_type(value, item) for item in expected):
            errors.append(f"{path}: expected one of {expected}")
            return
    elif isinstance(expected, str) and not _matches_type(value, expected):
        errors.append(f"{path}: expected {expected}, got {type(value).__name__}")
        return
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, dict):
        required = schema.get("required", [])
        for key in required if isinstance(required, list) else []:
            if isinstance(key, str) and key not in value:
                errors.append(f"{path}: missing required '{key}'")
        properties = schema.get("properties", {})
        properties = properties if isinstance(properties, dict) else {}
        for key, child in properties.items():
            if key in value and isinstance(child, dict):
                _check(value[key], child, f"{path}.{key}", errors)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}: unexpected property '{key}'")
        elif isinstance(additional, dict):
            for key, child_value in value.items():
                if key not in properties:
                    _check(child_value, additional, f"{path}.{key}", errors)
        max_properties = schema.get("maxProperties")
        if isinstance(max_properties, int) and len(value) > max_properties:
            errors.append(f"{path}: more than maxProperties ({max_properties})")
    elif isinstance(value, list):
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append(f"{path}: fewer than minItems ({min_items})")
        if isinstance(max_items, int) and len(value) > max_items:
            errors.append(f"{path}: more than maxItems ({max_items})")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _check(item, item_schema, f"{path}[{index}]", errors)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: below minimum ({minimum})")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: above maximum ({maximum})")
    if isinstance(value, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        pattern = schema.get("pattern")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append(f"{path}: shorter than minLength ({min_length})")
        if isinstance(max_length, int) and len(value) > max_length:
            errors.append(f"{path}: longer than maxLength ({max_length})")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path}: does not match pattern {pattern!r}")
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for child in all_of:
            if isinstance(child, dict):
                _check(value, child, path, errors)
    condition = schema.get("if")
    if isinstance(condition, dict):
        condition_errors: list[str] = []
        _check(value, condition, path, condition_errors)
        branch = schema.get("then") if not condition_errors else schema.get("else")
        if isinstance(branch, dict):
            _check(value, branch, path, errors)
