"""Phase 50 (0.50.0 N13): Cantrip.card_edits_standing toggle, migration 074,
CRUD + export/import roundtrip, assistant tool schema, and cache-friendly
standing vs dynamic card-field edit routing in both default and manual-order
passes."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import Base
from app.models.cantrip import Cantrip
from app.models.user import User
from app.services import cantrip as cantrip_service
from app.services.assistant.registry import TOOLS
from app.services.cantrip import process_cantrips
from app.services.cantrip_context import CardEditTracker
from app.services.deno_runner import CantripResult
from app.services.migrations import run_migrations
from app.services.pipeline_order import run_pre_forward_ordered

CARD_SYSTEM = (
    "Preset text.\n"
    "<Alice's Persona>Kind and brave.</Alice's Persona>\n"
    "<Scenario>A quiet tavern.</Scenario>\n"
    "<UserPersona>Traveler.</UserPersona>\n"
)


def _card_messages(user_text: str = "I order an ale.") -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": CARD_SYSTEM},
        {"role": "user", "content": user_text},
    ]


def _make_fake_runner(append_map: dict[str, str]):
    """Return an async mock for ``run_cantrip`` that appends ``append_map[code]``
    to ``context['character']['scenario']``."""

    async def _fake_run_cantrip(
        code: str,
        context: dict[str, Any],
        chat_data: dict[str, Any] | None = None,
        user_data: dict[str, Any] | None = None,
        cantrip_data: dict[str, Any] | None = None,
        timeout_ms: int = 5000,
        passenger_handler: Any = None,
    ) -> CantripResult:
        char = context["character"]
        scenario = char["scenario"]
        if code in append_map:
            scenario = scenario + append_map[code]
        return CantripResult(
            personality=char["personality"],
            scenario=scenario,
            example_dialogs=char["example_dialogs"],
            chat_data=dict(chat_data or {}),
            user_data=dict(user_data or {}),
            cantrip_data=dict(cantrip_data or {}),
        )

    return _fake_run_cantrip


# ============================================================================
# Migration 074 upgrade path
# ============================================================================


class TestMigration074CantripStanding:
    async def test_migration_074_adds_column_with_default_false(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("ALTER TABLE cantrips DROP COLUMN card_edits_standing;"))

            # Vacuity check: column is actually absent before migrating.
            async with engine.connect() as conn:
                cols = [r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(cantrips)")]
                assert "card_edits_standing" not in cols

            await run_migrations(engine)

            async with engine.connect() as conn:
                cols = [r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(cantrips)")]
                assert "card_edits_standing" in cols

            async with AsyncSession(engine, expire_on_commit=False) as session:
                user = User(username="u-mig074", password_hash="x", gitv_api_key="k-mig074")
                session.add(user)
                await session.flush()

                c = Cantrip(user_id=user.id, name="Default Cantrip")
                session.add(c)
                await session.commit()

                loaded = (
                    await session.execute(select(Cantrip).where(Cantrip.id == c.id))
                ).scalar_one()
                assert loaded.card_edits_standing is False
        finally:
            await engine.dispose()


# ============================================================================
# Router CRUD + Export/Import roundtrip & Assistant schema
# ============================================================================


class TestCantripStandingApiAndSchema:
    async def test_crud_and_export_import_roundtrip(self, admin_client):
        client, _token, _api_key = admin_client

        # Default is False
        resp_default = await client.post(
            "/api/cantrips",
            json={"name": "Dynamic Cantrip", "code": "// noop"},
        )
        assert resp_default.status_code == 201, resp_default.text
        assert resp_default.json()["card_edits_standing"] is False

        # Explicit True on create
        resp = await client.post(
            "/api/cantrips",
            json={
                "name": "Standing Cantrip",
                "code": 'context.character.scenario += "\\nStanding rule.";',
                "card_edits_standing": True,
            },
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        cid = created["id"]
        assert created["card_edits_standing"] is True

        # GET single & GET list both expose the flag
        get_resp = await client.get(f"/api/cantrips/{cid}")
        assert get_resp.status_code == 200
        assert get_resp.json()["card_edits_standing"] is True

        list_resp = await client.get("/api/cantrips")
        assert list_resp.status_code == 200
        by_id = {item["id"]: item for item in list_resp.json()["cantrips"]}
        assert by_id[cid]["card_edits_standing"] is True
        assert by_id[resp_default.json()["id"]]["card_edits_standing"] is False

        # Export includes card_edits_standing=True
        exp_resp = await client.get(f"/api/cantrips/{cid}/export")
        assert exp_resp.status_code == 200
        exported = json.loads(exp_resp.text)
        assert exported["card_edits_standing"] is True

        # Re-import exported payload via POST /api/cantrips preserves card_edits_standing
        exported["name"] = "Reimported Standing Cantrip"
        imp_resp = await client.post("/api/cantrips", json=exported)
        assert imp_resp.status_code == 201, imp_resp.text
        assert imp_resp.json()["card_edits_standing"] is True

        # PUT toggles card_edits_standing off and back on
        upd_off = await client.put(
            f"/api/cantrips/{cid}",
            json={"card_edits_standing": False},
        )
        assert upd_off.status_code == 200
        assert upd_off.json()["card_edits_standing"] is False

        upd_on = await client.put(
            f"/api/cantrips/{cid}",
            json={"card_edits_standing": True},
        )
        assert upd_on.status_code == 200
        assert upd_on.json()["card_edits_standing"] is True

    def test_assistant_tools_expose_card_edits_standing(self):
        create_tool = TOOLS["create_cantrip"]
        update_tool = TOOLS["update_cantrip"]
        assert "card_edits_standing" in create_tool.args_model.model_fields
        assert "card_edits_standing" in update_tool.args_model.model_fields
        assert "card_edits_standing" in create_tool.doc
        assert "card_edits_standing" in update_tool.doc


# ============================================================================
# CardEditTracker unit tests
# ============================================================================


class TestCardEditTracker:
    def test_standing_then_dynamic_and_dynamic_then_standing_both_separate_cleanly(self):
        base = {"personality": "P", "scenario": "B", "example_dialogs": ""}

        # Order 1: Standing then Dynamic
        t1 = CardEditTracker(base)
        t1.record_step(
            {"personality": "P", "scenario": "B", "example_dialogs": ""},
            {"personality": "P", "scenario": "B\nS", "example_dialogs": ""},
            is_standing=True,
        )
        t1.record_step(
            {"personality": "P", "scenario": "B\nS", "example_dialogs": ""},
            {"personality": "P", "scenario": "B\nS\nD", "example_dialogs": ""},
            is_standing=False,
        )
        standing1, tail1 = t1.resolve_for_cache_friendly()
        assert standing1["scenario"] == "B\nS"
        assert tail1 == ["D"]

        # Order 2: Dynamic then Standing
        t2 = CardEditTracker(base)
        t2.record_step(
            {"personality": "P", "scenario": "B", "example_dialogs": ""},
            {"personality": "P", "scenario": "B\nD", "example_dialogs": ""},
            is_standing=False,
        )
        t2.record_step(
            {"personality": "P", "scenario": "B\nD", "example_dialogs": ""},
            {"personality": "P", "scenario": "B\nD\nS", "example_dialogs": ""},
            is_standing=True,
        )
        standing2, tail2 = t2.resolve_for_cache_friendly()
        assert standing2["scenario"] == "B\nS"
        assert tail2 == ["D"]

    def test_full_replacement_supersedes_prior_dynamic_deltas(self):
        base = {"personality": "P", "scenario": "B", "example_dialogs": ""}
        t = CardEditTracker(base)
        t.record_step(
            {"personality": "P", "scenario": "B", "example_dialogs": ""},
            {"personality": "P", "scenario": "B\nD", "example_dialogs": ""},
            is_standing=False,
        )
        t.record_step(
            {"personality": "P", "scenario": "B\nD", "example_dialogs": ""},
            {"personality": "P", "scenario": "Completely replaced", "example_dialogs": ""},
            is_standing=True,
        )
        standing, tail = t.resolve_for_cache_friendly()
        assert standing["scenario"] == "Completely replaced"
        assert tail == []


# ============================================================================
# Runtime: process_cantrips & run_pre_forward_ordered under cache_friendly=True
# ============================================================================


async def _seed_user_and_cantrips(
    cantrip_specs: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    async with cantrip_service.async_session() as db:
        user = User(username="u-standing-rt", password_hash="x", gitv_api_key="k-standing-rt")
        db.add(user)
        await db.flush()
        ids: list[str] = []
        for idx, spec in enumerate(cantrip_specs):
            c = Cantrip(
                user_id=user.id,
                name=spec["name"],
                code=spec["code"],
                is_active=True,
                run_pre_driver=True,
                execution_order=spec.get("execution_order", (idx + 1) * 10),
                card_edits_standing=spec.get("card_edits_standing", False),
            )
            db.add(c)
            await db.flush()
            ids.append(c.id)
        await db.commit()
        return user.id, ids


class TestProcessCantripsStandingRuntime:
    async def test_standing_cantrip_stays_in_system_prompt(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({"STANDING": "\nStanding rule: no magic."}),
        )
        user_id, _ = await _seed_user_and_cantrips(
            [{"name": "Standing", "code": "STANDING", "card_edits_standing": True}]
        )
        body = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        out = await process_cantrips(body, user_id, {})
        msgs = out["messages"]
        assert "<Scenario>A quiet tavern.\nStanding rule: no magic.</Scenario>" in msgs[0]["content"]
        assert msgs[1]["content"] == "I order an ale."

    async def test_dynamic_cantrip_moves_to_user_tail(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({"DYNAMIC": "\nHP: 42"}),
        )
        user_id, _ = await _seed_user_and_cantrips(
            [{"name": "Dynamic", "code": "DYNAMIC", "card_edits_standing": False}]
        )
        body = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        out = await process_cantrips(body, user_id, {})
        msgs = out["messages"]
        assert msgs[0]["content"] == CARD_SYSTEM
        assert msgs[1]["content"] == "I order an ale.\n\nHP: 42"

    async def test_mixed_standing_then_dynamic(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({
                "STANDING": "\nStanding lore.",
                "DYNAMIC": "\nTurn 5 HUD.",
            }),
        )
        user_id, _ = await _seed_user_and_cantrips([
            {"name": "Standing", "code": "STANDING", "execution_order": 10, "card_edits_standing": True},
            {"name": "Dynamic", "code": "DYNAMIC", "execution_order": 20, "card_edits_standing": False},
        ])
        body = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        out = await process_cantrips(body, user_id, {})
        msgs = out["messages"]
        assert "<Scenario>A quiet tavern.\nStanding lore.</Scenario>" in msgs[0]["content"]
        assert "Turn 5 HUD." not in msgs[0]["content"]
        assert msgs[1]["content"] == "I order an ale.\n\nTurn 5 HUD."

    async def test_mixed_dynamic_then_standing(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({
                "DYNAMIC": "\nTurn 5 HUD.",
                "STANDING": "\nStanding lore.",
            }),
        )
        user_id, _ = await _seed_user_and_cantrips([
            {"name": "Dynamic", "code": "DYNAMIC", "execution_order": 10, "card_edits_standing": False},
            {"name": "Standing", "code": "STANDING", "execution_order": 20, "card_edits_standing": True},
        ])
        body = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        out = await process_cantrips(body, user_id, {})
        msgs = out["messages"]
        assert "<Scenario>A quiet tavern.\nStanding lore.</Scenario>" in msgs[0]["content"]
        assert "Turn 5 HUD." not in msgs[0]["content"]
        assert msgs[1]["content"] == "I order an ale.\n\nTurn 5 HUD."

    async def test_cache_friendly_false_splices_both_in_place(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({
                "DYNAMIC": "\nTurn 5 HUD.",
                "STANDING": "\nStanding lore.",
            }),
        )
        user_id, _ = await _seed_user_and_cantrips([
            {"name": "Dynamic", "code": "DYNAMIC", "execution_order": 10, "card_edits_standing": False},
            {"name": "Standing", "code": "STANDING", "execution_order": 20, "card_edits_standing": True},
        ])
        body = {"_gitv_cache_friendly": False, "messages": _card_messages()}
        out = await process_cantrips(body, user_id, {})
        msgs = out["messages"]
        assert "<Scenario>A quiet tavern.\nTurn 5 HUD.\nStanding lore.</Scenario>" in msgs[0]["content"]
        assert msgs[1]["content"] == "I order an ale."


class TestPipelineOrderStandingRuntime:
    async def test_manual_order_mixed_both_orders(self, monkeypatch):
        monkeypatch.setattr(
            cantrip_service,
            "run_cantrip",
            _make_fake_runner({
                "DYNAMIC": "\nTurn 5 HUD.",
                "STANDING": "\nStanding lore.",
            }),
        )
        user_id, ids = await _seed_user_and_cantrips([
            {"name": "Dynamic", "code": "DYNAMIC", "execution_order": 10, "card_edits_standing": False},
            {"name": "Standing", "code": "STANDING", "execution_order": 20, "card_edits_standing": True},
        ])
        dyn_id, stand_id = ids

        # Order A: Dynamic then Standing
        plan_a = {
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": dyn_id},
                {"type": "cantrip", "id": stand_id},
            ],
        }
        body_a = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        await run_pre_forward_ordered(plan_a, body_a, user_id, {})
        assert "<Scenario>A quiet tavern.\nStanding lore.</Scenario>" in body_a["messages"][0]["content"]
        assert "Turn 5 HUD." not in body_a["messages"][0]["content"]
        assert body_a["messages"][1]["content"] == "I order an ale.\n\nTurn 5 HUD."

        # Order B: Standing then Dynamic
        plan_b = {
            "enabled": True,
            "items": [
                {"type": "cantrip", "id": stand_id},
                {"type": "cantrip", "id": dyn_id},
            ],
        }
        body_b = {"_gitv_cache_friendly": True, "messages": _card_messages()}
        await run_pre_forward_ordered(plan_b, body_b, user_id, {})
        assert "<Scenario>A quiet tavern.\nStanding lore.</Scenario>" in body_b["messages"][0]["content"]
        assert "Turn 5 HUD." not in body_b["messages"][0]["content"]
        assert body_b["messages"][1]["content"] == "I order an ale.\n\nTurn 5 HUD."
