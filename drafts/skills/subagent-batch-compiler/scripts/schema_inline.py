"""Utilities to dereference JSON Schema $defs/$ref and apply domain bounds."""

from __future__ import annotations

import copy
from typing import Any


def inline_json_schema(
    schema: dict[str, Any],
    *,
    bound_overrides: dict[tuple[str, ...], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inline all $defs/$ref nodes and optionally tighten nested property constraints.

    Args:
        schema: Raw JSON schema dictionary (e.g., from Pydantic `model_json_schema()`).
        bound_overrides: Optional mapping from dotted/tuple property path inside
            `properties` to constraint dict updates, e.g.
            `{("difficulty", "S_chain_length", "score"): {"maximum": 3}}`.
    """
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, list):
            return [_resolve(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref_name = str(node["$ref"]).split("/")[-1]
            target = copy.deepcopy(defs[ref_name])
            for k, v in node.items():
                if k != "$ref":
                    target[k] = v
            return _resolve(target)
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "$defs":
                continue
            out[k] = _resolve(v)
        return out

    resolved = _resolve(copy.deepcopy(schema))

    if bound_overrides:
        for path_tuple, patch in bound_overrides.items():
            cur: Any = resolved
            for seg in path_tuple:
                props = cur.get("properties") if isinstance(cur, dict) else None
                if not isinstance(props, dict) or seg not in props:
                    cur = None
                    break
                cur = props[seg]
            if isinstance(cur, dict):
                cur.update(patch)

    return resolved
