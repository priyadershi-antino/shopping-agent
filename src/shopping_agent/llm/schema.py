"""Strict JSON schemas for structured output.

Providers that enforce schemas (Claude `output_config.format`, OpenAI/Gemini `json_schema`)
want: no `$ref`s, `additionalProperties: false` and every property listed in `required`
(optional fields stay nullable). Pydantic's schema is converted to that shape here.
"""

import copy
from typing import Any

from pydantic import BaseModel

_DROP_KEYS = {"title", "default", "examples"}


def _simplify_any_of(options: list[Any]) -> list[Any]:
    """Pydantic renders Decimal as number-or-pattern-string; keep just the number."""
    has_number = any(isinstance(o, dict) and o.get("type") == "number" for o in options)
    if not has_number:
        return options
    return [
        o
        for o in options
        if not (isinstance(o, dict) and o.get("type") == "string" and "pattern" in o)
    ]


def _resolve(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 30:
        raise ValueError("schema nesting too deep (recursive model?)")
    if isinstance(node, list):
        return [_resolve(item, defs, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
        return _resolve(merged, defs, depth + 1)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS or key == "$defs":
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _resolve(prop, defs, depth + 1) for name, prop in value.items()}
        elif key == "anyOf" and isinstance(value, list):
            out[key] = _resolve(_simplify_any_of(value), defs, depth + 1)
        else:
            out[key] = _resolve(value, defs, depth + 1)
    if isinstance(out.get("anyOf"), list) and len(out["anyOf"]) == 1:
        out = {**{k: v for k, v in out.items() if k != "anyOf"}, **out["anyOf"][0]}
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    resolved: dict[str, Any] = _resolve(schema, defs)
    return resolved


def tool_parameters(model: type[BaseModel]) -> dict[str, Any]:
    """Tool input schema: refs inlined, but optional arguments stay optional."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    resolved: dict[str, Any] = _inline_only(schema, defs)
    return resolved


def _inline_only(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    if depth > 30:
        raise ValueError("schema nesting too deep")
    if isinstance(node, list):
        return [_inline_only(item, defs, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].rsplit("/", 1)[-1]
        merged = {**copy.deepcopy(defs[name]), **{k: v for k, v in node.items() if k != "$ref"}}
        return _inline_only(merged, defs, depth + 1)
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in {"$defs", "title"}:
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {n: _inline_only(p, defs, depth + 1) for n, p in value.items()}
        elif key == "anyOf" and isinstance(value, list):
            out[key] = _inline_only(_simplify_any_of(value), defs, depth + 1)
        else:
            out[key] = _inline_only(value, defs, depth + 1)
    if isinstance(out.get("anyOf"), list) and len(out["anyOf"]) == 1:
        out = {**{k: v for k, v in out.items() if k != "anyOf"}, **out["anyOf"][0]}
    return out
