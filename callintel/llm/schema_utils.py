"""Pydantic -> provider-ready JSON schema.

Produces a dereferenced schema that satisfies OpenAI strict Structured Outputs
(every property required, additionalProperties=false, no unsupported keywords) and
that Ollama / vLLM grammar-constrained decoding accepts. Field constraints dropped
here are still enforced afterwards by pydantic validation.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

from ..utils import sha256_text

_DROP = {"title", "default", "examples", "minLength", "maxLength", "pattern", "format", "minimum", "maximum",
         "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "uniqueItems", "discriminator"}


_LIMITS = {"minItems", "maxItems"}


def _strict(node: Any, defs: dict[str, Any], keep_limits: bool = False) -> Any:
    if isinstance(node, list):
        return [_strict(x, defs, keep_limits) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        return _strict(copy.deepcopy(defs[name]), defs, keep_limits)
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _DROP and not (keep_limits and k in _LIMITS):
            continue
        if k == "properties":
            out[k] = {pk: _strict(pv, defs, keep_limits) for pk, pv in v.items()}
        else:
            out[k] = _strict(v, defs, keep_limits)
    if "const" in out:
        out["enum"] = [out.pop("const")]
    if out.get("type") == "object" or "properties" in out:
        props = out.setdefault("properties", {})
        out["type"] = "object"
        out["required"] = list(props.keys())
        out["additionalProperties"] = False
    return out


@lru_cache(maxsize=512)
def _cached(model: type[BaseModel], keep_limits: bool = False) -> tuple[str, str]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    strict = _strict(schema, defs, keep_limits)
    # keep declared field order: grammar-constrained decoders emit keys in schema order,
    # and generation quality depends on it (decide type -> cite line -> copy quote -> summarise)
    text = json.dumps(strict)
    return text, sha256_text(text)[:16]


def strict_schema(model: type[BaseModel], keep_limits: bool = False) -> dict[str, Any]:
    """keep_limits=True keeps minItems/maxItems (enforced by grammar decoders such as Ollama)."""
    return json.loads(_cached(model, keep_limits)[0])


def schema_hash(model: type[BaseModel]) -> str:
    return _cached(model, False)[1]


def minimal_instance(schema: dict[str, Any]) -> Any:
    """Smallest valid value for a (strict) schema - used by the mock provider."""
    if "enum" in schema:
        vals = schema["enum"]
        for pref in ("unclear", "other", "not_applicable", "neutral"):
            if pref in vals:
                return pref
        return vals[0]
    if "anyOf" in schema:
        types = [s.get("type") for s in schema["anyOf"]]
        if "null" in types:
            return None
        return minimal_instance(schema["anyOf"][0])
    t = schema.get("type")
    if t == "object":
        return {k: minimal_instance(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return []
    if t == "string":
        return ""
    if t == "integer":
        return 3
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None
