from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator, SchemaError


def validate_schema_definition(schema: dict[str, Any]) -> str | None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return exc.message
    return None


def validate_json_schema(data: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    validator = Draft202012Validator(schema)
    errors = []
    for error in sorted(validator.iter_errors(data), key=lambda item: list(item.path)):
        errors.append(
            {
                "path": list(error.absolute_path),
                "schema_path": list(error.absolute_schema_path),
                "message": error.message,
            }
        )
    return errors
