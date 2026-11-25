from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.endpoint import Endpoint
from app.models.map import MapStage
from app.models.user import User
from app.models.verification import VerificationRule
from app.services.debug import init_debug
from app.services.llm_params import (
    PARAM_TYPES,
    ParamDef,
    ParameterDef,
    apply_to_body,
    coerce,
    resolve,
)
from app.services.map_pipeline import _resolve_stage_endpoint
from app.services.passenger import _resolve_chain
from app.services.routing import _build_failover_chain, resolve_endpoints_by_tag
from app.services.verification import check_response
from tests.conftest import TestSessionLocal


async def _seed_user(db, username: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash="not-a-real-hash",
        gitv_api_key=f"hash-{username}-{uuid.uuid4().hex[:8]}",
    )
    db.add(user)
    await db.flush()
    return user


def _make_endpoint(
    user_id: str,
    name: str,
    *,
    role_tag: str = "default",
    custom_tag: str = "",
    priority: int = 1,
    enabled: bool = True,
) -> Endpoint:
    return Endpoint(
        id=str(uuid.uuid4()),
        user_id=user_id,
        name=name,
        base_url=f"https://{name.lower().replace(' ', '-')}.test",
        api_key="sk-test",
        role_tag=role_tag,
        custom_tag=custom_tag,
        priority=priority,
        enabled=enabled,
    )


# ============================================================================
# N10: Custom role_tag routing & failover isolation
# ============================================================================


@pytest.mark.asyncio
async def test_resolve_endpoints_by_tag_custom_and_cross_user_isolation():
    async with TestSessionLocal() as db:
        alice = await _seed_user(db, "alice")
        bob = await _seed_user(db, "bob")

        ep_local_2 = _make_endpoint(
            alice.id, "Alice Local 2", role_tag="custom", custom_tag="local", priority=2
        )
        ep_local_1 = _make_endpoint(
            alice.id, "Alice Local 1", role_tag="custom", custom_tag="local", priority=1
        )
        ep_local_disabled = _make_endpoint(
            alice.id, "Alice Local Off", role_tag="custom", custom_tag="local", priority=0, enabled=False
        )
        ep_cheap = _make_endpoint(
            alice.id, "Alice Cheap", role_tag="custom", custom_tag="cheap", priority=1
        )
        ep_driver = _make_endpoint(
            alice.id, "Alice Driver", role_tag="driver", priority=1
        )
        ep_bob_local = _make_endpoint(
            bob.id, "Bob Local", role_tag="custom", custom_tag="local", priority=1
        )
        db.add_all([ep_local_2, ep_local_1, ep_local_disabled, ep_cheap, ep_driver, ep_bob_local])
        await db.commit()

        # Custom tag resolves Alice's enabled "local" endpoints in priority order only
        matched_local = await resolve_endpoints_by_tag(db, alice.id, "local")
        assert [ep.name for ep in matched_local] == ["Alice Local 1", "Alice Local 2"]
        assert all(ep.user_id == alice.id for ep in matched_local)

        # Rule 23: Bob's query sees only Bob's "local" endpoint, never Alice's
        bob_local = await resolve_endpoints_by_tag(db, bob.id, "local")
        assert [ep.name for ep in bob_local] == ["Bob Local"]

        # Bob querying "cheap" sees nothing (Alice's "cheap" is isolated)
        assert await resolve_endpoints_by_tag(db, bob.id, "cheap") == []

        # Preset tag still resolves normally
        matched_driver = await resolve_endpoints_by_tag(db, alice.id, "driver")
        assert [ep.name for ep in matched_driver] == ["Alice Driver"]

        # Empty / whitespace tag returns empty list
        assert await resolve_endpoints_by_tag(db, alice.id, "") == []
        assert await resolve_endpoints_by_tag(db, alice.id, "   ") == []


@pytest.mark.asyncio
async def test_build_failover_chain_isolates_custom_tags():
    async with TestSessionLocal() as db:
        alice = await _seed_user(db, "alice_failover")

        ep_local_1 = _make_endpoint(
            alice.id, "Local Primary", role_tag="custom", custom_tag="local", priority=1
        )
        ep_local_2 = _make_endpoint(
            alice.id, "Local Backup", role_tag="custom", custom_tag="local", priority=2
        )
        ep_cheap = _make_endpoint(
            alice.id, "Cheap Ep", role_tag="custom", custom_tag="cheap", priority=1
        )
        ep_driver_1 = _make_endpoint(
            alice.id, "Driver 1", role_tag="driver", priority=1
        )
        ep_driver_2 = _make_endpoint(
            alice.id, "Driver 2", role_tag="driver", priority=2
        )
        db.add_all([ep_local_1, ep_local_2, ep_cheap, ep_driver_1, ep_driver_2])
        await db.commit()

        # "custom"/"local" chains only with other "custom"/"local" endpoints
        local_chain = await _build_failover_chain(db, ep_local_1, alice.id)
        assert [c.endpoint_name for c in local_chain] == ["Local Primary", "Local Backup"]

        # "custom"/"cheap" is isolated from "custom"/"local"
        cheap_chain = await _build_failover_chain(db, ep_cheap, alice.id)
        assert [c.endpoint_name for c in cheap_chain] == ["Cheap Ep"]

        # Preset tag failover continues to chain same-role_tag endpoints
        driver_chain = await _build_failover_chain(db, ep_driver_1, alice.id)
        assert [c.endpoint_name for c in driver_chain] == ["Driver 1", "Driver 2"]


@pytest.mark.asyncio
async def test_passenger_tag_fallback_emits_debug_capture():
    async with TestSessionLocal() as db:
        alice = await _seed_user(db, "alice_passenger")
        ep_default = _make_endpoint(alice.id, "Default Ep", role_tag="default", priority=1)
        ep_local = _make_endpoint(
            alice.id, "Local Ep", role_tag="custom", custom_tag="local", priority=2
        )
        db.add_all([ep_default, ep_local])
        await db.commit()

        # Missing tag -> falls back to default chain and records debug capture
        body_miss: dict = {"messages": []}
        init_debug(body_miss, [])
        chain_miss = await _resolve_chain(db, alice.id, "nonexistent", body_json=body_miss)
        assert [c.endpoint_name for c in chain_miss] == ["Default Ep"]
        stages = body_miss["_gitv_debug"]["stages"]
        fallback_stages = [s for s in stages if s["name"] == "passenger_tag_fallback"]
        assert len(fallback_stages) == 1
        assert fallback_stages[0]["metadata"]["requested_tag"] == "nonexistent"
        assert "nonexistent" in fallback_stages[0]["detail"]

        # Matching custom tag -> resolves directly with NO fallback capture
        body_hit: dict = {"messages": []}
        init_debug(body_hit, [])
        chain_hit = await _resolve_chain(db, alice.id, "local", body_json=body_hit)
        assert [c.endpoint_name for c in chain_hit] == ["Local Ep"]
        hit_fallbacks = [
            s for s in body_hit["_gitv_debug"]["stages"] if s["name"] == "passenger_tag_fallback"
        ]
        assert hit_fallbacks == []


@pytest.mark.asyncio
async def test_map_stage_tag_fallback_emits_debug_capture():
    async with TestSessionLocal() as db:
        alice = await _seed_user(db, "alice_map")
        ep_default = _make_endpoint(alice.id, "Default Ep", role_tag="default", priority=1)
        ep_local = _make_endpoint(
            alice.id, "Local Ep", role_tag="custom", custom_tag="local", priority=2
        )
        db.add_all([ep_default, ep_local])
        await db.commit()

        stage_miss = MapStage(
            id=str(uuid.uuid4()),
            map_id=str(uuid.uuid4()),
            name="Drafting",
            stage_order=0,
            endpoint_tag="missing_tag",
        )
        body_miss: dict = {"messages": []}
        init_debug(body_miss, [])
        endpoints_miss, _ = await _resolve_stage_endpoint(
            db, stage_miss, alice.id, body_json=body_miss
        )
        assert [ep.name for ep in endpoints_miss] == ["Default Ep"]
        fallback_stages = [
            s for s in body_miss["_gitv_debug"]["stages"] if s["name"] == "map_stage_tag_fallback"
        ]
        assert len(fallback_stages) == 1
        assert fallback_stages[0]["metadata"] == {
            "stage": "Drafting",
            "requested_tag": "missing_tag",
        }
        assert "Drafting" in fallback_stages[0]["detail"]
        assert "missing_tag" in fallback_stages[0]["detail"]

        # Matching custom tag -> resolves directly with NO fallback capture
        stage_hit = MapStage(
            id=str(uuid.uuid4()),
            map_id=str(uuid.uuid4()),
            name="LocalStage",
            stage_order=0,
            endpoint_tag="local",
        )
        body_hit: dict = {"messages": []}
        init_debug(body_hit, [])
        endpoints_hit, _ = await _resolve_stage_endpoint(
            db, stage_hit, alice.id, body_json=body_hit
        )
        assert [ep.name for ep in endpoints_hit] == ["Local Ep"]
        hit_fallbacks = [
            s for s in body_hit["_gitv_debug"]["stages"] if s["name"] == "map_stage_tag_fallback"
        ]
        assert hit_fallbacks == []


# ============================================================================
# N11: "object" (JSON) parameter type
# ============================================================================


def test_object_param_type_valid_string_and_dict_apply_to_body():
    assert "object" in PARAM_TYPES

    # JSON string validates and coerces to nested dict
    p_str = ParameterDef(
        name="chat_template_kwargs",
        type="object",
        value='{"thinking": true, "budget": 1024}',
    )
    assert p_str.type == "object"

    resolved = resolve([
        (
            "endpoint",
            [ParamDef(name="chat_template_kwargs", type="object", value=p_str.value)],
        )
    ])
    body = apply_to_body({"model": "qwen3", "messages": []}, resolved)
    assert body["chat_template_kwargs"] == {"thinking": True, "budget": 1024}

    # Dict value validates and coerces to a fresh copy
    original = {"thinking": True, "nested": {"k": "v"}}
    p_dict = ParameterDef(name="chat_template_kwargs", type="object", value=original)
    assert p_dict.value == original

    param_def = ParamDef(name="chat_template_kwargs", type="object", value=original)
    ok, coerced = coerce(param_def)
    assert ok is True
    assert coerced == original
    coerced["nested"]["k"] = "mutated"
    assert original["nested"]["k"] == "v"

    # Optional blank values are allowed when not required
    assert ParameterDef(name="extra_opts", type="object", value=None).value is None
    assert ParameterDef(name="extra_opts", type="object", value="").value == ""
    assert ParameterDef(name="extra_opts", type="object", value="   ").value == "   "


@pytest.mark.parametrize(
    "bad_value",
    [
        "{not valid json",
        "[1, 2, 3]",
        "42",
        "true",
        '"just a string"',
        [],
        [1, 2],
        42,
        True,
    ],
)
def test_object_param_type_rejects_invalid_values(bad_value):
    with pytest.raises(ValidationError):
        ParameterDef(name="chat_template_kwargs", type="object", value=bad_value)


@pytest.mark.parametrize("bad_value", ["{bad", "[1, 2]", "123", None, [1], 42, True])
def test_object_param_coerce_drops_invalid_without_raising(bad_value):
    ok, value = coerce(ParamDef(name="chat_template_kwargs", type="object", value=bad_value))
    assert ok is False
    assert value is None


# ============================================================================
# 0.48.1 Review Fix: invalid regex with no judge endpoint records errored=True
# ============================================================================


@pytest.mark.asyncio
async def test_verification_invalid_regex_without_endpoint_records_errored():
    bad_rule = VerificationRule(
        id="rule-bad-regex",
        user_id="user-1",
        name="Broken Regex Rule",
        description="Has an unclosed character class",
        prompt="Check tone",
        regex_pattern="[unclosed",
        is_active=True,
        max_retries=1,
        execution_order=1,
        resubmission_strategy="add_instructions",
    )

    result = await check_response(
        content="Hello world",
        rules=[bad_rule],
        endpoint=None,
        model="",
    )

    assert len(result.judgments) == 1
    judgment = result.judgments[0]
    assert judgment.errored is True
    assert judgment.violation is False
    assert "Invalid regex pattern" in judgment.reason
    assert "Regex pre-check passed" not in judgment.reason
    assert "an invalid regex pattern and no available endpoint" in judgment.thinking
    assert len(result.errors) == 1


@pytest.mark.asyncio
async def test_verification_valid_nonmatching_regex_without_endpoint_passes_cleanly():
    good_rule = VerificationRule(
        id="rule-good-regex",
        user_id="user-1",
        name="Clean Regex Rule",
        description="Valid regex that does not match",
        prompt="Check tone",
        regex_pattern=r"\bforbidden\b",
        is_active=True,
        max_retries=1,
        execution_order=1,
        resubmission_strategy="add_instructions",
    )

    result = await check_response(
        content="Hello world",
        rules=[good_rule],
        endpoint=None,
        model="",
    )

    assert len(result.judgments) == 1
    judgment = result.judgments[0]
    assert judgment.errored is False
    assert judgment.violation is False
    assert "Regex pre-check passed" in judgment.reason
    assert result.errors == []
