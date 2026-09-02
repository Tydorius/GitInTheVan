"""Tool schema derivation (Phase 26b).

Derivation, not snapshots: the assertions are about how a schema is built from
a route template plus a router's Pydantic model, so a field added to a router
does not need an edit here, and a broken inliner does.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.services.assistant.registry import TOOLS, Risk, Tool
from app.services.assistant.schema import path_param_names, schemas_for, tool_schema


class _Nested(BaseModel):
    label: str
    weight: int = 1


class _Body(BaseModel):
    name: str
    note: str = ""
    nested: _Nested | None = None
    items: list[_Nested] = Field(default_factory=list)


def _tool(**kwargs):
    base = {
        "name": "probe",
        "group": "cantrips",
        "risk": Risk.WRITE,
        "summary": "Probe.",
        "method": "PUT",
        "path": "/api/probe/{thing_id}",
        "args_model": _Body,
        "path_params": {"thing_id": "The thing."},
    }
    base.update(kwargs)
    return Tool(**base)


class TestPathParams:
    def test_extracts_placeholders_in_order(self):
        assert path_param_names("/a/{one}/b/{two}") == ["one", "two"]

    def test_no_placeholders(self):
        assert path_param_names("/api/cantrips") == []

    def test_path_params_are_required_strings(self):
        schema = tool_schema(_tool())["function"]["parameters"]
        assert schema["properties"]["thing_id"]["type"] == "string"
        assert schema["properties"]["thing_id"]["description"] == "The thing."
        assert "thing_id" in schema["required"]


class TestModelDerivation:
    def test_body_fields_appear(self):
        props = tool_schema(_tool())["function"]["parameters"]["properties"]
        assert "name" in props
        assert "note" in props

    def test_required_body_fields_are_required(self):
        schema = tool_schema(_tool())["function"]["parameters"]
        assert "name" in schema["required"]
        assert "note" not in schema["required"]

    def test_defs_are_inlined(self):
        rendered = str(tool_schema(_tool()))
        assert "$ref" not in rendered
        assert "$defs" not in rendered

    def test_nested_model_fields_survive_inlining(self):
        props = tool_schema(_tool())["function"]["parameters"]["properties"]
        rendered = str(props["nested"])
        assert "label" in rendered and "weight" in rendered

    def test_list_of_models_is_inlined(self):
        props = tool_schema(_tool())["function"]["parameters"]["properties"]
        assert "label" in str(props["items"])

    def test_titles_are_dropped(self):
        assert "'title'" not in str(tool_schema(_tool()))

    def test_query_params_are_included(self):
        tool = _tool(query_params={"limit": "How many."})
        props = tool_schema(tool)["function"]["parameters"]["properties"]
        assert props["limit"]["description"] == "How many."
        assert "limit" not in tool_schema(tool)["function"]["parameters"]["required"]

    def test_path_param_wins_over_a_body_field_of_the_same_name(self):
        class Clash(BaseModel):
            thing_id: int = 0

        props = tool_schema(_tool(args_model=Clash))["function"]["parameters"]["properties"]
        assert props["thing_id"]["type"] == "string"


class TestEnvelope:
    def test_function_envelope(self):
        schema = tool_schema(_tool())
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "probe"
        assert schema["function"]["description"]
        assert schema["function"]["parameters"]["type"] == "object"

    def test_description_falls_back_to_summary(self):
        schema = tool_schema(_tool(doc=""))
        assert schema["function"]["description"] == "Probe."

    def test_tool_with_no_args_model_has_empty_properties(self):
        schema = tool_schema(_tool(args_model=None, path="/api/probe", path_params={}))
        assert schema["function"]["parameters"]["properties"] == {}
        assert "required" not in schema["function"]["parameters"]

    def test_schemas_for_preserves_order(self):
        tools = [TOOLS["list_cantrips"], TOOLS["get_cantrip"]]
        names = [s["function"]["name"] for s in schemas_for(tools)]
        assert names == ["list_cantrips", "get_cantrip"]


class TestRealRegistry:
    def test_every_registered_tool_produces_a_schema(self):
        for tool in TOOLS.values():
            schema = tool_schema(tool)
            assert schema["function"]["name"] == tool.name
            assert isinstance(schema["function"]["parameters"]["properties"], dict)

    def test_no_registered_schema_contains_a_ref(self):
        rendered = str(schemas_for(list(TOOLS.values())))
        assert "$ref" not in rendered

    def test_update_cantrip_takes_the_router_model_fields(self):
        from app.routers.cantrips import CantripUpdate

        props = tool_schema(TOOLS["update_cantrip"])["function"]["parameters"]["properties"]
        for field in CantripUpdate.model_fields:
            assert field in props, field

    def test_path_and_query_reach_export_debug_run(self):
        props = tool_schema(TOOLS["export_debug_run"])["function"]["parameters"]["properties"]
        assert "exchange_id" in props
        assert "format" in props
