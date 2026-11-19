"""Pipeline ordering (0.41.0): manual interleaved order over the pre-forward
window + the context.strip() prompt-strip output.

Coverage per the design doc §5: parse/ordered_steps units, the
apply_prompt_strips rewriter (scopes, content parts, compile degradation),
the settings API round-trip, the default-path pin (no plan = today's
behavior), the manual-mode E2E in the maintainer's exact shape (C1, L1,
L2, C2 + unlisted C3/L3 — C2 sees L1+L2's entries, L2 does not match the
keyword C1 stripped), rule 19 (the list never activates), strip
continuity-neutrality, the Prompt Janitor template, and the migration-061
upgrade path with a vacuity check.

E2E tests require Deno (real cantrip executions through the proxy).
"""

from __future__ import annotations

import json

import pytest

from app.services.deno_runner import DENO_PATH
from app.services.pipeline_order import MAX_PLAN_ITEMS, ordered_steps, parse_plan

requires_deno = pytest.mark.skipif(not DENO_PATH, reason="Deno not available")

MOCK_ENDPOINT_URL = "http://pipeline-order-e2e.test"


# ============================================================================
# Helpers
# ============================================================================

async def _make_endpoint(client):
    resp = await client.post("/api/endpoints", json={
        "name": "Pipeline Order E2E",
        "base_url": MOCK_ENDPOINT_URL,
        "api_key": "upstream-key",
        "enabled": True,
    })
    assert resp.status_code == 201, resp.text
    await client.put("/api/settings", json={"default_endpoint_id": resp.json()["id"]})


def _upstream(httpx_mock, content: str = "OK"):
    httpx_mock.add_response(
        url=f"{MOCK_ENDPOINT_URL}/v1/chat/completions",
        json={
            "id": "t", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    )


async def _send(client, api_key, content="hello", conv="po-chat", messages=None):
    return await client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": messages or [{"role": "user", "content": content}]},
        headers={"Authorization": f"Bearer {api_key}", "x-conversation-id": conv},
    )


def _forwarded_text(httpx_mock) -> str:
    requests = httpx_mock.get_requests()
    return json.dumps(json.loads(requests[-1].content)["messages"])


async def _make_cantrip(client, code, name, **overrides):
    payload = {"name": name, "code": code, "is_active": True}
    payload.update(overrides)
    resp = await client.post("/api/cantrips", json=payload)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _make_lorebook(client, name, entries, **overrides):
    payload = {"name": name}
    payload.update(overrides)
    resp = await client.post("/api/lorebooks", json=payload)
    assert resp.status_code in (200, 201), resp.text
    lb_id = resp.json()["id"]
    for entry in entries:
        resp = await client.post(f"/api/lorebooks/{lb_id}/entries", json=entry)
        assert resp.status_code == 201, resp.text
    return lb_id


async def _set_plan(client, enabled, items):
    resp = await client.put("/api/settings", json={
        "pipeline_order": {"enabled": enabled, "items": items}
    })
    assert resp.status_code == 200, resp.text
    return resp.json()


# Code snippets: each records what it saw into its cantrip_data so the
# assertion side can read execution order and context out of the store.
SEE_LOREBOOKS_CODE = """
const seen = (context.chat.last_messages || [])
    .map(m => m.message).join("\\n");
context.cantrip_data.set("saw", seen);
"""

STRIP_CODE = """
context.strip("DRAGONFIRE_MARKER", "all");
"""


# ============================================================================
# Unit: parse_plan / ordered_steps
# ============================================================================

class TestParsePlan:
    def test_empty_and_corrupt_degrade_to_disabled(self):
        assert parse_plan(None) == {"enabled": False, "items": []}
        assert parse_plan("") == {"enabled": False, "items": []}
        assert parse_plan("{not json") == {"enabled": False, "items": []}
        assert parse_plan("[1,2]") == {"enabled": False, "items": []}

    def test_shape_filtering(self):
        plan = parse_plan(json.dumps({
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": "a"},
                {"type": "bogus", "id": "b"},
                {"type": "lorebook", "id": ""},
                "nonsense",
                {"type": "lorebook", "id": "c"},
            ],
        }))
        assert plan["items"] == [
            {"type": "cantrip", "id": "a"},
            {"type": "lorebook", "id": "c"},
        ]

    def test_enabled_requires_items(self):
        plan = parse_plan('{"enabled": true, "items": []}')
        assert plan["enabled"] is False


class TestOrderedSteps:
    class _C:
        def __init__(self, id, name="c", execution_order=10):
            self.id, self.name, self.execution_order = id, name, execution_order

    class _L:
        def __init__(self, id, name="l", created_at=0):
            self.id, self.name, self.created_at = id, name, created_at

    def test_listed_order_then_unlisted_tail(self):
        c1, c2, c3 = self._C("c1"), self._C("c2"), self._C("c3", execution_order=1)
        l1, l2, l3 = self._L("l1"), self._L("l2"), self._L("l3", created_at=5)
        plan = {"enabled": True, "items": [
            {"type": "cantrip", "id": "c1"},
            {"type": "lorebook", "id": "l1"},
            {"type": "lorebook", "id": "l2"},
            {"type": "cantrip", "id": "c2"},
        ]}
        steps = ordered_steps(plan, [c1, c2, c3], [l1, l2, l3])
        # The listed interleaving, then the tail: unlisted cantrips by
        # execution_order, unlisted lorebooks by creation order.
        assert [(k, o.id) for k, o in steps] == [
            ("cantrip", "c1"), ("lorebook", "l1"), ("lorebook", "l2"),
            ("cantrip", "c2"),
            ("cantrip", "c3"), ("lorebook", "l3"),
        ]

    def test_plan_never_adds_nonactivating(self):
        # c_missing did not activate (not in the activating list); a plan
        # entry naming it produces no step. The list orders, never activates.
        plan = {"enabled": True, "items": [{"type": "cantrip", "id": "ghost"}]}
        steps = ordered_steps(plan, [self._C("real")], [])
        assert [(k, o.id) for k, o in steps] == [("cantrip", "real")]


# ============================================================================
# Unit: apply_prompt_strips
# ============================================================================

class TestApplyPromptStrips:
    def test_scopes_and_parts(self):
        from app.services.cantrip import apply_prompt_strips

        body = {"messages": [
            {"role": "system", "content": "keep [NOTE] drop"},
            {"role": "user", "content": [
                {"type": "text", "text": "before [NOTE] after"},
            ]},
        ]}
        applied = apply_prompt_strips(body, [{"pattern": r"\s*\[NOTE\]", "scope": "all"}])
        assert applied == 1
        assert body["messages"][0]["content"] == "keep drop"
        assert body["messages"][1]["content"][0]["text"] == "before after"

        body = {"messages": [
            {"role": "system", "content": "X_MARKER"},
            {"role": "user", "content": "X_MARKER"},
        ]}
        apply_prompt_strips(body, [{"pattern": "X_MARKER", "scope": "messages"}])
        assert body["messages"][0]["content"] == "X_MARKER"   # system untouched
        assert body["messages"][1]["content"] == ""

        apply_prompt_strips(body, [{"pattern": "X_MARKER", "scope": "system"}])
        assert body["messages"][0]["content"] == ""

    def test_bad_regex_degrades_to_skip(self):
        from app.services.cantrip import apply_prompt_strips

        body = {"messages": [{"role": "user", "content": "text"}]}
        applied = apply_prompt_strips(body, [{"pattern": "(unclosed", "scope": "all"}])
        assert applied == 0
        assert body["messages"][0]["content"] == "text"


# ============================================================================
# API: settings round-trip
# ============================================================================

class TestSettingsApi:
    @pytest.mark.asyncio
    async def test_round_trip_and_normalization(self, admin_client):
        client, _, _ = admin_client
        cantrip = await _make_cantrip(client, "// x", "ordered")
        lorebook = await _make_lorebook(client, "Book", [
            {"name": "e", "content": "c", "keys": ["k"]},
        ])
        body = await _set_plan(client, True, [
            {"type": "cantrip", "id": cantrip["id"]},
            {"type": "lorebook", "id": lorebook},
        ])
        assert body["pipeline_order"]["enabled"] is True
        assert body["pipeline_order"]["items"] == [
            {"type": "cantrip", "id": cantrip["id"]},
            {"type": "lorebook", "id": lorebook},
        ]

        # Corrupt shapes are refused; the good value survives.
        resp = await client.put("/api/settings", json={
            "pipeline_order": {"enabled": True, "items": "not a list"}
        })
        assert resp.status_code == 422
        resp = await client.put("/api/settings", json={
            "pipeline_order": {"enabled": True,
                               "items": [{"type": "cantrip", "id": "x"}] * (MAX_PLAN_ITEMS + 1)}
        })
        assert resp.status_code == 422
        assert (await client.get("/api/settings")).json()["pipeline_order"]["items"] == [
            {"type": "cantrip", "id": cantrip["id"]},
            {"type": "lorebook", "id": lorebook},
        ]

        # Saving an empty list stores off.
        body = await _set_plan(client, True, [])
        assert body["pipeline_order"]["enabled"] is False


# ============================================================================
# E2E: default path unchanged, manual order interleaves
# ============================================================================

@pytest.mark.asyncio
@requires_deno
class TestPipelineE2E:
    async def test_default_path_cantrip_sees_lorebook_entries(self, admin_client, httpx_mock):
        """No plan = today's behavior: the lorebook pass injects BEFORE the
        pre-driver cantrips, so the cantrip's last_messages contain the
        entry."""
        client, _, api_key = admin_client
        await _make_endpoint(client)
        await _make_lorebook(client, "PreBook", [
            {"name": "e", "content": "PREBOOK_ENTRY_TEXT", "is_constant": True},
        ])
        await _make_cantrip(client, SEE_LOREBOOKS_CODE, "Seer")
        _upstream(httpx_mock)
        resp = await _send(client, api_key)
        assert resp.status_code == 200, resp.text

        from sqlalchemy import select

        from app.models.cantrip_data import CantripData
        from tests.conftest import TestSessionLocal
        async with TestSessionLocal() as s:
            row = (await s.execute(select(CantripData).where(CantripData.key == "saw"))).scalar_one()
        assert "PREBOOK_ENTRY_TEXT" in json.loads(row.value_json)

    async def test_manual_order_interleaves_and_strip_gates_matching(
        self, admin_client, httpx_mock
    ):
        """The maintainer's exact example: C1 (strips a marker), L1, L2, C2
        (records what it saw), plus unlisted C3 and L3. C2 must see L1's
        entry; L2 must NOT match the keyword C1 stripped out of the prompt;
        the forwarded request must not contain the stripped text; the
        unlisted resources must still run."""
        client, _, api_key = admin_client
        await _make_endpoint(client)

        c1 = await _make_cantrip(client, STRIP_CODE, "Stripper", execution_order=1)
        # L1 is constant (fires regardless of matching), L2 keys on the
        # marker the strip removes.
        l1 = await _make_lorebook(client, "L1", [
            {"name": "const", "content": "L1_CONSTANT_ENTRY", "is_constant": True},
        ])
        l2 = await _make_lorebook(client, "L2", [
            {"name": "keyed", "content": "L2_KEYED_ENTRY", "keys": ["DRAGONFIRE_MARKER"]},
        ])
        c2 = await _make_cantrip(client, SEE_LOREBOOKS_CODE, "Witness", execution_order=5)
        await _make_cantrip(
            client, 'context.cantrip_data.set("c3_ran", true);', "TailCantrip",
            execution_order=9,
        )
        await _make_lorebook(client, "L3", [
            {"name": "tail", "content": "L3_TAIL_ENTRY", "is_constant": True},
        ])

        await _set_plan(client, True, [
            {"type": "cantrip", "id": c1["id"]},
            {"type": "lorebook", "id": l1},
            {"type": "lorebook", "id": l2},
            {"type": "cantrip", "id": c2["id"]},
        ])

        _upstream(httpx_mock)
        resp = await _send(client, api_key, content="hello DRAGONFIRE_MARKER")
        assert resp.status_code == 200, resp.text

        from sqlalchemy import select

        from app.models.cantrip_data import CantripData
        from tests.conftest import TestSessionLocal
        async with TestSessionLocal() as s:
            rows = {
                r.key: json.loads(r.value_json)
                for r in (await s.execute(select(CantripData))).scalars()
            }
        saw = rows.get("saw", "")
        # C2 ran AFTER L1's slot: it sees L1's constant entry...
        assert "L1_CONSTANT_ENTRY" in saw
        # ...but NOT L3 (unlisted lorebooks inject after the listed block,
        # so the tail's entries land after every listed cantrip).
        assert "L3_TAIL_ENTRY" not in saw
        # L2 did not match: its keyword was stripped before its slot.
        assert "L2_KEYED_ENTRY" not in saw
        # The strip removed the marker from the forwarded prompt...
        assert "DRAGONFIRE_MARKER" not in _forwarded_text(httpx_mock)
        # ...while the unlisted cantrip still ran.
        assert rows.get("c3_ran") is True
        # And the injected entries DID reach the model.
        assert "L1_CONSTANT_ENTRY" in _forwarded_text(httpx_mock)

    async def test_plan_never_activates_inactive_cantrip(self, admin_client, httpx_mock):
        """A listed but inactive, untagged cantrip does not run (rule 19:
        the list orders, it never activates)."""
        client, _, api_key = admin_client
        await _make_endpoint(client)
        # Inactive but TAGGED: the loader's rule-19 OR (is_active OR tag)
        # lets it reach activation, and only the hierarchy decides. A plan
        # entry naming it must not switch it on without its tag.
        ghost = await _make_cantrip(
            client, 'context.cantrip_data.set("ghost_ran", true);', "Ghost",
            is_active=False, tag="ghost-tag",
        )
        await _set_plan(client, True, [{"type": "cantrip", "id": ghost["id"]}])
        _upstream(httpx_mock)
        resp = await _send(client, api_key)
        assert resp.status_code == 200, resp.text

        from sqlalchemy import select

        from app.models.cantrip_data import CantripData
        from tests.conftest import TestSessionLocal
        async with TestSessionLocal() as s:
            ran = (await s.execute(
                select(CantripData).where(CantripData.key == "ghost_ran")
            )).scalar_one_or_none()
        assert ran is None

    async def test_strips_are_continuity_neutral(self, admin_client, httpx_mock):
        """Strips never touch the conversation-hash anchor: with an active
        strip cantrip, turn 2 chains onto turn 1's conversation -- the
        chat_data written in turn 1 is still there in turn 2. (Message
        shapes follow the canonical continuity test: a system message and
        >= 2 messages on turn 1, the client-visible reply in turn 2's
        history.)"""
        client, _, api_key = admin_client
        await _make_endpoint(client)
        witness = await _make_cantrip(client, STRIP_CODE + """
if (context.chat.message_count <= 2) {
    context.chat_data.set("turn1_marked", true);
}
context.cantrip_data.set("chained", context.chat_data.get("turn1_marked") === true && context.chat.last_messages.length > 2);
""", "ChainWitness")
        # Strips under the manual order (the orchestrator's at-slot path).
        await _set_plan(client, True, [{"type": "cantrip", "id": witness["id"]}])

        _upstream(httpx_mock)
        turn1 = {"model": "m", "messages": [
            {"role": "system", "content": "You are a narrator."},
            {"role": "user", "content": "alpha DRAGONFIRE_MARKER"},
        ]}
        # No conversation header: chat_data chains through the internal
        # rolling-hash identity, so an orphaned conversation loses it.
        first = await client.post(
            "/v1/chat/completions", json=turn1,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert first.status_code == 200, first.text

        _upstream(httpx_mock)
        turn2 = {"model": "m", "messages": [
            {"role": "system", "content": "You are a narrator."},
            {"role": "user", "content": "alpha DRAGONFIRE_MARKER"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "beta DRAGONFIRE_MARKER"},
        ]}
        second = await client.post(
            "/v1/chat/completions", json=turn2,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        assert second.status_code == 200, second.text

        from sqlalchemy import select

        from app.models.cantrip_data import CantripData
        from tests.conftest import TestSessionLocal
        async with TestSessionLocal() as s:
            row = (await s.execute(
                select(CantripData).where(CantripData.key == "chained")
            )).scalar_one()
        # Turn 2's run saw turn 1's chat_data (the conversation chained)
        # despite the strip firing on both turns.
        assert json.loads(row.value_json) is True
        # The strip fired both turns: neither forwarded prompt carries the
        # marker.
        for request in httpx_mock.get_requests():
            assert "DRAGONFIRE_MARKER" not in json.dumps(json.loads(request.content)["messages"])


# ============================================================================
# Template
# ============================================================================

class TestPromptJanitorTemplate:
    def test_present_and_ships_disabled(self):
        from app.services.deno_runner import validate_cantrip_syntax
        from app.templates.cantrip_templates import get_templates

        template = next(
            (t for t in get_templates() if t["name"] == "Prompt Janitor"), None
        )
        assert template is not None
        assert template.get("is_active") is False
        assert template.get("execution_order") == 1
        assert validate_cantrip_syntax(template["code"]) == ""


# ============================================================================
# Upgrade path: migration 061 on the old schema
# ============================================================================

class TestMigration061UpgradePath:
    @pytest.mark.asyncio
    async def test_column_added_on_upgrade(self):
        """user_settings is NOT a migration-created table, so
        TestMigrationCoverage cannot catch a missing 061 ALTER for it --
        only this test can (the skills.budget_weight bug class, rule 10).
        Simulate the pre-061 database: create_all from current models,
        then drop the column; vacuity-check; migrate; read it back through
        the ORM the way load_plan does."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.base import Base
        from app.services.migrations import run_migrations

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(
                    text("ALTER TABLE user_settings DROP COLUMN pipeline_order_json")
                )

            # Vacuity check: this really reproduces the old schema.
            async with engine.connect() as conn:
                cols = await conn.execute(text("PRAGMA table_info(user_settings)"))
                col_names = {r[1] for r in cols.all()}
            assert "pipeline_order_json" not in col_names

            await run_migrations(engine)

            from sqlalchemy import insert, select

            from app.models.user_settings import UserSettings
            async with engine.begin() as conn:
                await conn.execute(insert(UserSettings).values(
                    user_id="u-order", pipeline_order_json='{"enabled": true, "items": []}'))
                raw = (await conn.execute(
                    select(UserSettings.pipeline_order_json).where(
                        UserSettings.user_id == "u-order")
                )).scalar_one()
            assert json.loads(raw)["enabled"] is True
        finally:
            await engine.dispose()
