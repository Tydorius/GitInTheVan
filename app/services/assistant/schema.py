"""JSON schemas for the tools array, derived from the routers' own models.

Nothing is hand-written or snapshotted: a tool's parameters are its path
placeholders, its declared query parameters, and its router request model's
own JSON schema. A field added to a router body reaches the model without any
edit here.

Two transformations are applied to Pydantic's output. `$defs` are inlined,
because several providers reject `$ref` in a function schema, and `title` keys
are dropped, because they are noise the model pays for on every turn.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.assistant.registry import Tool

_PATH_PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Depth guard for the inliner. A self-referential model would otherwise expand
# forever; the schema is for a model to read, not to validate against, so a
# truncated branch is better than a hang.
_MAX_DEPTH = 8


def path_param_names(path: str) -> list[str]:
    """The `{name}` placeholders in a route template, in order."""
    return _PATH_PARAM.findall(path or "")


def _inline(node: Any, defs: dict[str, Any], depth: int = 0) -> Any:
    """Resolve `$ref` against `$defs` and strip `title` keys."""
    if depth > _MAX_DEPTH:
        return {}
    if isinstance(node, list):
        return [_inline(item, defs, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node

    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = defs.get(ref.split("/")[-1])
        if target is None:
            return {}
        merged = _inline(target, defs, depth + 1)
        # Keys alongside a $ref (description, default) stay and win.
        extra = {k: _inline(v, defs, depth + 1) for k, v in node.items() if k != "$ref"}
        if isinstance(merged, dict):
            return {**merged, **extra}
        return merged

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("title", "$defs"):
            continue
        out[key] = _inline(value, defs, depth + 1)
    return out


def _args_model_schema(model: Any) -> tuple[dict[str, Any], list[str]]:
    """Properties and required names from a Pydantic model, `$defs` inlined."""
    if model is None:
        return {}, []
    try:
        raw = model.model_json_schema()
    except Exception:  # pragma: no cover - a model that cannot describe itself
        return {}, []
    defs = raw.get("$defs", {}) or {}
    props = _inline(raw.get("properties", {}) or {}, defs)
    required = [str(name) for name in (raw.get("required") or [])]
    return props, required


def tool_schema(tool: Tool) -> dict[str, Any]:
    """One entry of the OpenAI `tools` array."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name in path_param_names(tool.path):
        properties[name] = {
            "type": "string",
            "description": tool.path_params.get(name, f"{name} path parameter."),
        }
        required.append(name)

    for name, description in (tool.query_params or {}).items():
        if name in properties:
            continue
        properties[name] = {"type": "string", "description": description}

    model_props, model_required = _args_model_schema(tool.args_model)
    for name, spec in model_props.items():
        if name in properties:
            # A path placeholder already owns the name; the body copy is noise.
            continue
        properties[name] = spec
    for name in model_required:
        if name not in required and name in properties:
            required.append(name)

    parameters: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        parameters["required"] = required

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.doc or tool.summary).strip(),
            "parameters": parameters,
        },
    }


def schemas_for(tools: list[Tool]) -> list[dict[str, Any]]:
    """Schemas for a list of tools, in the order given."""
    return [tool_schema(tool) for tool in tools]
