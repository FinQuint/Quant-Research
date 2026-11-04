"""A small JSON-schema subset validator, so model outputs are checked without a new dependency.

Supported keywords: type, properties, required, additionalProperties (true/false only), items, enum,
minItems, maxItems, minLength, minimum, maximum. Anything else in a schema is refused, so a
schema never silently checks less than it says.
"""

SUPPORTED = {"type", "properties", "required", "additionalProperties", "items", "enum", "minItems",
             "maxItems", "minLength", "minimum", "maximum", "description", "title"}
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    """The schema itself uses something this validator does not support."""


def check_schema(schema, path="$"):
    if not isinstance(schema, dict):
        raise SchemaError(f"{path}: schema must be an object")
    unknown = set(schema) - SUPPORTED
    if unknown:
        raise SchemaError(f"{path}: unsupported schema keywords {sorted(unknown)}")
    kinds = schema.get("type")
    for kind in (kinds if isinstance(kinds, list) else [kinds] if kinds else []):
        if kind not in _TYPES:
            raise SchemaError(f"{path}: unknown type {kind!r}")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise SchemaError(f"{path}: additionalProperties must be true or false")
    for name, sub in schema.get("properties", {}).items():
        check_schema(sub, f"{path}.{name}")
    if "items" in schema:
        check_schema(schema["items"], f"{path}[]")


def validate(value, schema, path="$"):
    """Return a list of error strings; empty means valid."""
    errors = []
    kinds = schema.get("type")
    if kinds is not None:
        kinds = kinds if isinstance(kinds, list) else [kinds]
        if not any(_TYPES[k](value) for k in kinds):
            return [f"{path}: expected {' or '.join(kinds)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(f"{path}: missing required field {name!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            for name in sorted(set(value) - set(properties)):
                errors.append(f"{path}: unexpected field {name!r}")
        for name, sub in properties.items():
            if name in value:
                errors += validate(value[name], sub, f"{path}.{name}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, str) and len(value) < schema.get("minLength", 0):
        errors.append(f"{path}: shorter than {schema['minLength']} characters")
    if _TYPES["number"](value):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above maximum {schema['maximum']}")
    return errors
