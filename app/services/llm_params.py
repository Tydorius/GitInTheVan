"""Typed, layered LLM request parameters.

GitInTheVan can name a model in seven places but had no way to say how to call
it. This module is the one definition of what a parameter is, how the layers
merge, and how a merged set is written onto an outbound request body.

Two rules govern everything here:

1. The closest layer to the message wins. Client payload is the base; user
   settings, endpoint, model and finally the call site (rule, map stage) each
   overwrite it. A verification rule's reasoning_effort beats the endpoint's for
   that rule's judge call, and both beat whatever the client sent.
2. Nothing here may raise on the proxy hot path. A corrupt blob, an uncoercible
   value or an unknown type degrades to "that parameter is absent" with a log
   line. Every request in the product flows through resolve(); a parse error
   must never become a 500.

The functions are pure -- no DB session, no I/O -- so callers load the JSON
columns themselves and pass the parsed layers in.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# The selectable types. `number` and `float` both coerce to a Python float and
# exist as separate labels only because both were asked for; the UI says so.
PARAM_TYPES: tuple[str, ...] = (
    "string",
    "string[]",
    "number",
    "float",
    "integer",
    "boolean",
)

# Names a parameter may never take. `model` has dedicated override fields at
# every scope, and `stream` is owned by the pipeline -- verification and
# driver-callable force it to false, so a map stage setting it would break the
# request in a way nothing reports. Enforced twice, in the Pydantic validator
# and again in apply_to_body(), because these values also arrive through map
# pack imports.
RESERVED_NAMES: frozenset[str] = frozenset({"messages", "model", "stream"})

NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")

MAX_PARAMS_PER_SCOPE = 32
MAX_BLOB_CHARS = 8192


@dataclass(frozen=True)
class ParamDef:
    """One parameter as stored. Frozen because a resolved layer is shared
    across a failover chain and must not be mutated by one candidate."""

    name: str
    type: str
    value: Any
    description: str = ""
    required: bool = False
    options: tuple[str, ...] = ()


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, tuple)) and not value:
        return True
    return False


class ParameterDef(BaseModel):
    """API shape for a parameter. Shared by every router that accepts one.

    Lives beside the service rather than in a router because five routers need
    it and the project declares schemas inline per router with no shared
    schemas package.
    """

    name: str
    type: str = "string"
    value: Any = None
    description: str = ""
    required: bool = False
    options: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def check_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not NAME_PATTERN.match(v):
            raise ValueError(
                "Parameter name must start with a letter or underscore and contain "
                "only letters, digits, underscore, dot or hyphen (max 64 characters)"
            )
        if v in RESERVED_NAMES or v.startswith("_gitv"):
            raise ValueError(f"'{v}' is a reserved parameter name")
        return v

    @field_validator("type")
    @classmethod
    def check_type(cls, v: str) -> str:
        if v not in PARAM_TYPES:
            raise ValueError(
                f"Unknown parameter type '{v}'. Expected one of {', '.join(PARAM_TYPES)}"
            )
        return v

    @field_validator("options")
    @classmethod
    def clean_options(cls, v: list[str]) -> list[str]:
        return [o.strip() for o in (v or []) if o and o.strip()]

    @field_validator("description")
    @classmethod
    def clean_description(cls, v: str) -> str:
        return (v or "").strip()

    @model_validator(mode="after")
    def check_value(self) -> ParameterDef:
        if self.required and _is_blank(self.value):
            raise ValueError(f"Parameter '{self.name}' is marked required and needs a value")
        if self.options and not _is_blank(self.value) and str(self.value) not in self.options:
            raise ValueError(
                f"Parameter '{self.name}' value '{self.value}' is not one of its "
                f"options ({', '.join(self.options)})"
            )
        return self

    def to_def(self) -> ParamDef:
        return ParamDef(
            name=self.name,
            type=self.type,
            value=self.value,
            description=self.description,
            required=self.required,
            options=tuple(self.options),
        )


def validate_params(items: Any, scope: str = "parameters") -> list[ParameterDef]:
    """Validate an incoming parameter list. Raises ValueError on any problem.

    Routers call this from a Pydantic validator so a bad payload becomes a 422;
    the pack importer calls it directly so an untrusted map cannot smuggle a
    reserved name past the API layer.
    """
    if items is None:
        return []
    if not isinstance(items, list):
        raise ValueError(f"{scope} must be a list")
    if len(items) > MAX_PARAMS_PER_SCOPE:
        raise ValueError(f"{scope} exceeds the limit of {MAX_PARAMS_PER_SCOPE} parameters")

    parsed = [item if isinstance(item, ParameterDef) else ParameterDef(**item) for item in items]

    seen: set[str] = set()
    for p in parsed:
        if p.name in seen:
            raise ValueError(f"Duplicate parameter name '{p.name}' in {scope}")
        seen.add(p.name)

    blob = dump_params(parsed)
    if len(blob) > MAX_BLOB_CHARS:
        raise ValueError(f"{scope} exceeds the size limit ({len(blob)} chars, max {MAX_BLOB_CHARS})")

    return parsed


def dump_params(items: list[Any]) -> str:
    """Serialize a validated parameter list to the stored JSON string."""
    return json.dumps(
        [
            {
                "name": p.name,
                "type": p.type,
                "value": p.value,
                "description": p.description,
                "required": p.required,
                "options": list(p.options),
            }
            for p in items
        ]
    )


def params_to_api(raw: str | None) -> list[ParameterDef]:
    """Stored JSON -> API shape.

    Uses the tolerant reader, so a blob written by an older version or edited by
    hand in the database degrades to the entries it can still make sense of
    rather than failing the whole response.
    """
    return [
        ParameterDef(
            name=p.name,
            type=p.type,
            value=p.value,
            description=p.description,
            required=p.required,
            options=list(p.options),
        )
        for p in parse_params(raw)
    ]


def params_from_api(items: Any, scope: str) -> str:
    """API shape -> stored JSON, validating on the way through.

    Raises HTTP 422 rather than 500 on a bad list. Shared by every router that
    accepts parameters so the error text is identical wherever it comes from.
    """
    from fastapi import HTTPException, status

    try:
        return dump_params(validate_params(items, scope))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def parse_params(raw: str | None, scope: str = "") -> list[ParamDef]:
    """Read a stored parameters_json column.

    Tolerant by design: malformed JSON, a non-list, a non-object entry or a
    missing name yields fewer parameters and a log line, never an exception.
    This runs on every proxied request.
    """
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except Exception:
        logger.warning("Ignoring malformed parameters JSON%s", f" on {scope}" if scope else "")
        return []

    if not isinstance(data, list):
        logger.warning(
            "Ignoring parameters JSON that is not a list%s", f" on {scope}" if scope else ""
        )
        return []

    defs: list[ParamDef] = []
    for entry in data[:MAX_PARAMS_PER_SCOPE]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name or name in RESERVED_NAMES or name.startswith("_gitv"):
            continue
        ptype = entry.get("type", "string")
        if ptype not in PARAM_TYPES:
            ptype = "string"
        options = entry.get("options") or []
        if not isinstance(options, list):
            options = []
        defs.append(
            ParamDef(
                name=name,
                type=ptype,
                value=entry.get("value"),
                description=str(entry.get("description", "")),
                required=bool(entry.get("required", False)),
                options=tuple(str(o) for o in options),
            )
        )
    return defs


def coerce(p: ParamDef) -> tuple[bool, Any]:
    """Coerce a stored value to its declared type.

    Returns (ok, value). A value that cannot be coerced is dropped rather than
    guessed at -- sending a string where a provider expects an integer produces
    a 400 the user cannot diagnose.
    """
    v = p.value

    try:
        if p.type == "boolean":
            if isinstance(v, bool):
                return True, v
            if isinstance(v, (int, float)):
                return True, bool(v)
            text = str(v).strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True, True
            if text in ("false", "0", "no", "off"):
                return True, False
            raise ValueError(text)

        if p.type == "integer":
            if isinstance(v, bool):
                raise ValueError("bool is not an integer")
            return True, int(str(v).strip())

        if p.type in ("number", "float"):
            if isinstance(v, bool):
                raise ValueError("bool is not a number")
            return True, float(str(v).strip())

        if p.type == "string[]":
            if isinstance(v, (list, tuple)):
                return True, [str(x) for x in v]
            text = str(v or "").strip()
            if not text:
                return True, []
            return True, [part.strip() for part in text.split(",") if part.strip()]

        if v is None:
            raise ValueError("no value")
        return True, str(v)

    except Exception:
        logger.warning(
            "Dropping parameter '%s': value %r cannot be coerced to %s", p.name, p.value, p.type
        )
        return False, None


@dataclass
class ResolvedParams:
    """The merged result plus where each key came from.

    Provenance is not decoration -- it is what the Debug timeline shows so a
    user can see which layer supplied a value, which is the whole point of a
    layered system.
    """

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.values)


def resolve(layers: list[tuple[str, list[ParamDef]]]) -> ResolvedParams:
    """Merge layers broadest-first. Later layers overwrite earlier ones per key.

    `layers` is an ordered list of (label, params). The label is what appears in
    the Debug capture, e.g. "endpoint" or "verification rule 'No purple prose'".
    """
    result = ResolvedParams()

    for label, params in layers:
        for p in params or []:
            ok, value = coerce(p)
            if not ok:
                continue
            if p.options and str(value) not in p.options:
                logger.warning(
                    "Dropping parameter '%s' from %s: value %r is not one of its options %s",
                    p.name,
                    label,
                    value,
                    list(p.options),
                )
                continue
            result.values[p.name] = value
            result.sources[p.name] = label

    return result


def apply_to_body(
    body: dict[str, Any], resolved: ResolvedParams | dict[str, Any]
) -> dict[str, Any]:
    """Return a copy of `body` with the resolved parameters written over it.

    Reserved names are skipped here as well as at validation, because a stored
    blob predating a validator change, or one arriving through a pack import
    path that missed the check, must still not be able to set `stream`.
    """
    values = resolved.values if isinstance(resolved, ResolvedParams) else (resolved or {})
    out = dict(body)
    for name, value in values.items():
        if name in RESERVED_NAMES or name.startswith("_gitv"):
            logger.warning("Refusing to apply reserved parameter '%s'", name)
            continue
        out[name] = value
    return out
