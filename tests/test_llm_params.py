"""Layered LLM parameters: precedence, coercion, and hot-path safety.

Two properties this file exists to pin:

1. The precedence order itself, including the maintainer's original example --
   `reasoning_effort: max` on the endpoint, `low` on a verification rule, and a
   client that sent `high` -- which is asserted by name below. Getting the layer
   order wrong is silent: the request still succeeds, just with the wrong model
   behaviour, and nothing in the product would report it.

2. That nothing in this module raises. `resolve()` runs on every proxied
   request. A corrupt `parameters_json` blob, a value that cannot be coerced, or
   an unknown type must degrade to "that parameter is absent", never to a 500.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.llm_params import (
    MAX_BLOB_CHARS,
    MAX_PARAMS_PER_SCOPE,
    ParamDef,
    ParameterDef,
    apply_to_body,
    coerce,
    dump_params,
    parse_params,
    resolve,
    validate_params,
)


def p(name, value, type="string", **kw):
    return ParamDef(name=name, type=type, value=value, **kw)


class TestPrecedence:
    def test_closest_layer_wins(self):
        resolved = resolve(
            [
                ("client", [p("reasoning_effort", "high")]),
                ("endpoint", [p("reasoning_effort", "max")]),
            ]
        )
        assert resolved.values["reasoning_effort"] == "max"
        assert resolved.sources["reasoning_effort"] == "endpoint"

    def test_endpoint_max_beaten_by_rule_low(self):
        """The original request's own example.

        Endpoint says max, a verification rule says low, the client asked for
        high. The judge call must send low.
        """
        resolved = resolve(
            [
                ("client", [p("reasoning_effort", "high")]),
                ("user settings", []),
                ("endpoint", [p("reasoning_effort", "max")]),
                ("model", []),
                ("verification rule 'No purple prose'", [p("reasoning_effort", "low")]),
            ]
        )
        assert resolved.values["reasoning_effort"] == "low"
        assert resolved.sources["reasoning_effort"] == "verification rule 'No purple prose'"

    def test_driver_call_keeps_endpoint_value(self):
        """Same config, driver call: the rule layer is absent, so max wins."""
        resolved = resolve(
            [
                ("client", [p("reasoning_effort", "high")]),
                ("endpoint", [p("reasoning_effort", "max")]),
                ("model", []),
            ]
        )
        assert resolved.values["reasoning_effort"] == "max"

    def test_model_layer_beats_endpoint(self):
        resolved = resolve(
            [
                ("endpoint", [p("max_tokens", 8000, type="integer")]),
                ("model", [p("max_tokens", 128000, type="integer")]),
            ]
        )
        assert resolved.values["max_tokens"] == 128000
        assert resolved.sources["max_tokens"] == "model"

    def test_layers_merge_rather_than_replace(self):
        """A narrower layer overrides only the keys it names."""
        resolved = resolve(
            [
                ("endpoint", [p("temperature", 0.7, type="float"), p("max_tokens", 4096, type="integer")]),
                ("map stage 'Dice'", [p("temperature", 0.1, type="float")]),
            ]
        )
        assert resolved.values == {"temperature": 0.1, "max_tokens": 4096}
        assert resolved.sources["temperature"] == "map stage 'Dice'"
        assert resolved.sources["max_tokens"] == "endpoint"

    def test_empty_layers_resolve_to_nothing(self):
        resolved = resolve([("endpoint", []), ("model", [])])
        assert resolved.values == {}
        assert not resolved


class TestCoercion:
    @pytest.mark.parametrize(
        "ptype,raw,expected",
        [
            ("string", "max", "max"),
            ("string", 42, "42"),
            ("integer", "128000", 128000),
            ("integer", 128000, 128000),
            ("float", "0.7", 0.7),
            ("number", "0.7", 0.7),
            ("number", 1, 1.0),
            ("boolean", "true", True),
            ("boolean", "off", False),
            ("boolean", True, True),
            ("boolean", 0, False),
        ],
    )
    def test_coerces_each_type(self, ptype, raw, expected):
        ok, value = coerce(p("x", raw, type=ptype))
        assert ok
        assert value == expected
        assert isinstance(value, type(expected))

    def test_string_array_from_comma_separated(self):
        ok, value = coerce(p("stop", "END, ###, <|eot|>", type="string[]"))
        assert ok
        assert value == ["END", "###", "<|eot|>"]

    def test_string_array_from_list(self):
        ok, value = coerce(p("stop", ["END", "###"], type="string[]"))
        assert ok
        assert value == ["END", "###"]

    def test_string_array_round_trips_through_storage(self):
        stored = dump_params(validate_params([{"name": "stop", "type": "string[]", "value": ["END", "###"]}]))
        ok, value = coerce(parse_params(stored)[0])
        assert ok
        assert value == ["END", "###"]

    @pytest.mark.parametrize(
        "ptype,raw",
        [
            ("integer", "not-a-number"),
            ("integer", True),
            ("float", "high"),
            ("number", False),
            ("boolean", "maybe"),
            ("string", None),
        ],
    )
    def test_uncoercible_value_is_dropped_not_raised(self, ptype, raw):
        ok, value = coerce(p("x", raw, type=ptype))
        assert ok is False
        assert value is None

    def test_dropped_parameter_does_not_reach_the_body(self):
        resolved = resolve([("endpoint", [p("max_tokens", "lots", type="integer")])])
        assert resolved.values == {}


class TestOptions:
    def test_value_outside_options_is_dropped_at_resolve(self):
        """A stored blob can hold a stale value if the options list was edited
        after the fact. Resolve drops it rather than sending it upstream."""
        resolved = resolve(
            [("endpoint", [p("reasoning_effort", "ultra", options=("low", "high", "max"))])]
        )
        assert resolved.values == {}

    def test_value_inside_options_survives(self):
        resolved = resolve(
            [("endpoint", [p("reasoning_effort", "high", options=("low", "high", "max"))])]
        )
        assert resolved.values["reasoning_effort"] == "high"

    def test_api_rejects_value_outside_options(self):
        with pytest.raises(ValidationError):
            ParameterDef(name="reasoning_effort", value="ultra", options=["low", "high", "max"])


class TestValidation:
    def test_required_blank_is_rejected(self):
        with pytest.raises(ValidationError):
            ParameterDef(name="reasoning_effort", value="", required=True)

    def test_required_with_value_is_accepted(self):
        assert ParameterDef(name="reasoning_effort", value="max", required=True).value == "max"

    def test_optional_blank_is_accepted(self):
        assert ParameterDef(name="reasoning_effort", value="", required=False).value == ""

    @pytest.mark.parametrize("name", ["messages", "model", "stream", "_gitv_tags", "_gitvfoo"])
    def test_reserved_names_are_rejected(self, name):
        with pytest.raises(ValidationError):
            ParameterDef(name=name, value="x")

    @pytest.mark.parametrize("name", ["", "9lives", "has space", "a" * 65, "bad$name"])
    def test_malformed_names_are_rejected(self, name):
        with pytest.raises(ValidationError):
            ParameterDef(name=name, value="x")

    def test_unknown_type_is_rejected(self):
        with pytest.raises(ValidationError):
            ParameterDef(name="x", type="datetime", value="x")

    def test_duplicate_names_are_rejected(self):
        with pytest.raises(ValueError, match="Duplicate"):
            validate_params([{"name": "temperature", "value": "1"}, {"name": "temperature", "value": "2"}])

    def test_parameter_count_cap(self):
        items = [{"name": f"p{i}", "value": "x"} for i in range(MAX_PARAMS_PER_SCOPE + 1)]
        with pytest.raises(ValueError, match="limit"):
            validate_params(items)

    def test_blob_size_cap(self):
        items = [{"name": "big", "value": "x" * (MAX_BLOB_CHARS + 100)}]
        with pytest.raises(ValueError, match="size limit"):
            validate_params(items)

    def test_non_list_is_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            validate_params({"name": "x"})

    def test_none_is_an_empty_list(self):
        assert validate_params(None) == []


class TestHotPathSafety:
    """parse_params runs on every proxied request. It must never raise."""

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "not json at all",
            "{",
            '{"name": "temperature"}',  # object, not a list
            '"a string"',
            "123",
            "null",
        ],
    )
    def test_malformed_blob_yields_empty_list(self, raw):
        assert parse_params(raw, scope="endpoint 'Test'") == []

    def test_non_object_entries_are_skipped(self):
        assert parse_params('["temperature", 5, null]') == []

    def test_entry_without_a_name_is_skipped(self):
        assert parse_params('[{"type": "integer", "value": 4}]') == []

    def test_unknown_stored_type_falls_back_to_string(self):
        assert parse_params('[{"name": "x", "type": "datetime", "value": "v"}]')[0].type == "string"

    def test_non_list_options_are_ignored(self):
        assert parse_params('[{"name": "x", "value": "v", "options": "low,high"}]')[0].options == ()

    def test_reserved_names_in_a_stored_blob_are_dropped(self):
        """Defence in depth: a blob written before the validator existed, or one
        that arrived through a pack import, must not be able to set stream."""
        parsed = parse_params('[{"name": "stream", "value": true, "type": "boolean"}]')
        assert parsed == []

    def test_stored_blob_is_capped_on_read(self):
        raw = dump_params(
            [ParamDef(name=f"p{i}", type="string", value="v") for i in range(MAX_PARAMS_PER_SCOPE + 10)]
        )
        assert len(parse_params(raw)) == MAX_PARAMS_PER_SCOPE


class TestApplyToBody:
    def test_configured_value_overwrites_the_client(self):
        body = {"model": "gpt-4", "messages": [], "reasoning_effort": "high"}
        out = apply_to_body(body, resolve([("endpoint", [p("reasoning_effort", "max")])]))
        assert out["reasoning_effort"] == "max"

    def test_client_keys_not_configured_survive(self):
        body = {"model": "gpt-4", "messages": [], "temperature": 0.9, "top_p": 0.4}
        out = apply_to_body(body, resolve([("endpoint", [p("max_tokens", 100, type="integer")])]))
        assert out["temperature"] == 0.9
        assert out["top_p"] == 0.4
        assert out["max_tokens"] == 100

    def test_source_body_is_not_mutated(self):
        body = {"model": "gpt-4"}
        apply_to_body(body, {"temperature": 0.1})
        assert body == {"model": "gpt-4"}

    def test_reserved_names_are_refused_even_if_they_reach_resolve(self):
        """resolve() does not filter reserved names -- parse_params and the
        validator do. This asserts the last line of defence independently."""
        body = {"model": "gpt-4", "stream": False, "messages": ["keep me"]}
        out = apply_to_body(body, {"stream": True, "model": "evil", "messages": [], "_gitv_tags": ["x"]})
        assert out["stream"] is False
        assert out["model"] == "gpt-4"
        assert out["messages"] == ["keep me"]
        assert "_gitv_tags" not in out

    def test_accepts_a_plain_dict(self):
        assert apply_to_body({}, {"temperature": 0.1})["temperature"] == 0.1

    def test_accepts_none_safely(self):
        assert apply_to_body({"a": 1}, None) == {"a": 1}


class TestStorageRoundTrip:
    def test_validate_dump_parse_preserves_every_field(self):
        original = [
            {
                "name": "reasoning_effort",
                "type": "string",
                "value": "max",
                "description": "Thinking budget",
                "required": True,
                "options": ["low", "high", "max"],
            }
        ]
        parsed = parse_params(dump_params(validate_params(original)))
        assert len(parsed) == 1
        got = parsed[0]
        assert got.name == "reasoning_effort"
        assert got.type == "string"
        assert got.value == "max"
        assert got.description == "Thinking budget"
        assert got.required is True
        assert got.options == ("low", "high", "max")

    def test_empty_list_round_trips(self):
        assert parse_params(dump_params(validate_params([]))) == []
