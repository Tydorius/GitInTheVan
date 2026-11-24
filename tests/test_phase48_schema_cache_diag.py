from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import Base
from app.models.debug_exchange import DebugExchange
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.cache_diagnostics import analyze_prompt_cache
from app.services.migrations import run_migrations
from tests.conftest import TestSessionLocal


class TestPromptCachingSettingsAPI:
    async def test_settings_get_put_roundtrip(self, admin_client):
        client, _token, _api_key = admin_client

        # Defaults are False
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_friendly_mode"] is False
        assert data["auto_cache_control"] is False

        # Enable cache_friendly_mode only
        resp = await client.put("/api/settings", json={"cache_friendly_mode": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_friendly_mode"] is True
        assert data["auto_cache_control"] is False

        # Enable auto_cache_control too
        resp = await client.put("/api/settings", json={"auto_cache_control": True})
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_friendly_mode"] is True
        assert data["auto_cache_control"] is True

        # Verify persisted via GET
        resp = await client.get("/api/settings")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_friendly_mode"] is True
        assert data["auto_cache_control"] is True

        # Toggle both back off
        resp = await client.put(
            "/api/settings",
            json={"cache_friendly_mode": False, "auto_cache_control": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cache_friendly_mode"] is False
        assert data["auto_cache_control"] is False


class TestMigration072UpgradePath:
    async def test_migration_072_adds_prompt_caching_columns(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("ALTER TABLE user_settings DROP COLUMN cache_friendly_mode;"))
                await conn.execute(text("ALTER TABLE user_settings DROP COLUMN auto_cache_control;"))

            # Vacuity check: both columns really absent before migration
            async with engine.connect() as conn:
                cols = [r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(user_settings)")]
                assert "cache_friendly_mode" not in cols
                assert "auto_cache_control" not in cols

            await run_migrations(engine)

            async with engine.connect() as conn:
                cols = [r[1] for r in await conn.exec_driver_sql("PRAGMA table_info(user_settings)")]
                assert "cache_friendly_mode" in cols
                assert "auto_cache_control" in cols

            async with AsyncSession(engine) as session:
                us = UserSettings(user_id="u-mig-072")
                session.add(us)
                await session.commit()

                loaded = (
                    await session.execute(
                        select(UserSettings).where(UserSettings.user_id == "u-mig-072")
                    )
                ).scalar_one()
                assert loaded.cache_friendly_mode is False
                assert loaded.auto_cache_control is False

                loaded.cache_friendly_mode = True
                loaded.auto_cache_control = True
                await session.commit()

                reloaded = (
                    await session.execute(
                        select(UserSettings).where(UserSettings.user_id == "u-mig-072")
                    )
                ).scalar_one()
                assert reloaded.cache_friendly_mode is True
                assert reloaded.auto_cache_control is True
        finally:
            await engine.dispose()


class TestAnalyzePromptCache:
    def test_no_previous_turn_returns_unavailable(self):
        curr = {
            "stages": [
                {
                    "name": "original",
                    "label": "Original",
                    "messages_before": None,
                    "messages_after": json.dumps([{"role": "system", "content": "Hello"}]),
                }
            ]
        }
        result = analyze_prompt_cache(curr, None)
        assert result["available"] is False
        assert "reason" in result

        result_empty = analyze_prompt_cache(curr, {"stages": []})
        assert result_empty["available"] is False

    def test_tail_only_new_user_message_is_cache_hot(self):
        long_system = "You are a helpful roleplay narrator. " * 40
        prev_msgs = [
            {"role": "system", "content": long_system},
            {"role": "user", "content": "I enter the tavern and look around."},
            {"role": "assistant", "content": "The tavern is warm and dimly lit by a hearth."},
        ]
        curr_msgs = [
            *prev_msgs,
            {"role": "user", "content": "I order an ale."},
        ]

        prev_pipeline = {
            "original_messages": json.dumps(prev_msgs),
            "stages": [
                {
                    "name": "forward",
                    "label": "Forward",
                    "messages_before": json.dumps(prev_msgs),
                    "messages_after": json.dumps(prev_msgs),
                }
            ],
        }
        curr_pipeline = {
            "original_messages": json.dumps(curr_msgs),
            "stages": [
                {
                    "name": "forward",
                    "label": "Forward",
                    "messages_before": json.dumps(curr_msgs),
                    "messages_after": json.dumps(curr_msgs),
                }
            ],
        }

        result = analyze_prompt_cache(curr_pipeline, prev_pipeline, previous_exchange_id="ex-1")
        assert result["available"] is True
        assert result["previous_exchange_id"] == "ex-1"
        assert result["is_tail_only"] is True
        assert result["culprit_stage"] == ""
        assert result["first_diff_message_index"] == 3
        assert result["first_diff_role"] == "user"
        assert result["prefix_stability_pct"] > 90.0
        assert result["shared_prefix_tokens"] > 0
        assert result["total_prompt_tokens"] >= result["shared_prefix_tokens"]
        assert "cache-eligible" in result["summary"]

    def test_system_message_mutated_by_memory_injection_attributes_culprit(self):
        base_system = "You are a helpful narrator."
        prev_system = base_system + "\n[Memory]: Player met the blacksmith."
        curr_system = base_system + "\n[Memory]: Player met the blacksmith and bought a sword."

        prev_msgs = [
            {"role": "system", "content": prev_system},
            {"role": "user", "content": "Hello " * 100},
            {"role": "assistant", "content": "Greetings " * 100},
        ]
        curr_before_mem = [
            {"role": "system", "content": base_system},
            {"role": "user", "content": "Hello " * 100},
            {"role": "assistant", "content": "Greetings " * 100},
            {"role": "user", "content": "What do I have in my pack?"},
        ]
        curr_after_mem = [
            {"role": "system", "content": curr_system},
            {"role": "user", "content": "Hello " * 100},
            {"role": "assistant", "content": "Greetings " * 100},
            {"role": "user", "content": "What do I have in my pack?"},
        ]

        prev_pipeline = {
            "stages": [
                {
                    "name": "memory_injection",
                    "label": "Memory Injection",
                    "messages_before": json.dumps(curr_before_mem[:3]),
                    "messages_after": json.dumps(prev_msgs),
                }
            ]
        }
        curr_pipeline = {
            "original_messages": json.dumps(curr_before_mem),
            "stages": [
                {
                    "name": "passthrough",
                    "label": "Passthrough",
                    "messages_before": json.dumps(curr_before_mem),
                    "messages_after": json.dumps(curr_before_mem),
                },
                {
                    "name": "memory_injection",
                    "label": "Memory Injection",
                    "messages_before": json.dumps(curr_before_mem),
                    "messages_after": json.dumps(curr_after_mem),
                },
            ],
        }

        result = analyze_prompt_cache(curr_pipeline, prev_pipeline, previous_exchange_id="ex-prev")
        assert result["available"] is True
        assert result["is_tail_only"] is False
        assert result["first_diff_message_index"] == 0
        assert result["first_diff_role"] == "system"
        assert result["culprit_stage"] == "Memory Injection"
        assert result["prefix_stability_pct"] < 20.0
        assert "messages[0]" in result["summary"]
        assert "Memory Injection" in result["summary"]

    def test_content_block_lists_normalized_equivalently(self):
        prev_msgs = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "Static system prompt." * 10, "cache_control": {"type": "ephemeral"}}],
            },
            {"role": "user", "content": "Turn 1"},
        ]
        curr_msgs = [
            {
                "role": "system",
                "content": [{"type": "text", "text": "Static system prompt." * 10}],
            },
            {"role": "user", "content": "Turn 1"},
            {"role": "user", "content": "Turn 2"},
        ]
        result = analyze_prompt_cache(
            {"stages": [{"name": "s", "messages_after": json.dumps(curr_msgs)}]},
            {"stages": [{"name": "s", "messages_after": json.dumps(prev_msgs)}]},
            previous_exchange_id="ex-block",
        )
        assert result["available"] is True
        assert result["is_tail_only"] is True
        assert result["first_diff_message_index"] == 2


class TestDebugRouterCacheAnalysis:
    async def test_debug_detail_endpoint_returns_cache_analysis(self, admin_client):
        client, _token, _api_key = admin_client

        async with TestSessionLocal() as db:
            user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()

            base_time = datetime.now(UTC) - timedelta(minutes=5)
            prev_msgs = [
                {"role": "system", "content": "System instructions.\nMemory: A"},
                {"role": "user", "content": "First message " * 40},
                {"role": "assistant", "content": "First reply " * 40},
            ]
            curr_before = [
                {"role": "system", "content": "System instructions."},
                {"role": "user", "content": "First message " * 40},
                {"role": "assistant", "content": "First reply " * 40},
                {"role": "user", "content": "Second message"},
            ]
            curr_after = [
                {"role": "system", "content": "System instructions.\nMemory: A + B"},
                {"role": "user", "content": "First message " * 40},
                {"role": "assistant", "content": "First reply " * 40},
                {"role": "user", "content": "Second message"},
            ]

            ex1 = DebugExchange(
                user_id=user.id,
                chat_id="chat-cache-diag-1",
                model="test-model",
                pipeline_data=json.dumps({
                    "schema_version": 2,
                    "original_messages": json.dumps(prev_msgs),
                    "stages": [
                        {
                            "name": "memory_injection",
                            "label": "Memory Injection",
                            "messages_before": json.dumps(prev_msgs),
                            "messages_after": json.dumps(prev_msgs),
                        }
                    ],
                }),
                response_content="First reply",
                verification_data="{}",
                created_at=base_time,
            )
            ex2 = DebugExchange(
                user_id=user.id,
                chat_id="chat-cache-diag-1",
                model="test-model",
                pipeline_data=json.dumps({
                    "schema_version": 2,
                    "original_messages": json.dumps(curr_before),
                    "stages": [
                        {
                            "name": "memory_injection",
                            "label": "Memory Injection",
                            "messages_before": json.dumps(curr_before),
                            "messages_after": json.dumps(curr_after),
                        }
                    ],
                }),
                response_content="Second reply",
                verification_data="{}",
                created_at=base_time + timedelta(seconds=30),
            )
            db.add_all([ex1, ex2])
            await db.commit()
            ex1_id, ex2_id = ex1.id, ex2.id

        # First exchange has no predecessor
        resp1 = await client.get(f"/api/debug/{ex1_id}")
        assert resp1.status_code == 200
        body1 = resp1.json()
        assert body1["cache_analysis"] is not None
        assert body1["cache_analysis"]["available"] is False

        # Second exchange compares against ex1 and catches Memory Injection mutating messages[0]
        resp2 = await client.get(f"/api/debug/{ex2_id}")
        assert resp2.status_code == 200
        body2 = resp2.json()
        ca = body2["cache_analysis"]
        assert ca is not None
        assert ca["available"] is True
        assert ca["previous_exchange_id"] == ex1_id
        assert ca["is_tail_only"] is False
        assert ca["first_diff_message_index"] == 0
        assert ca["culprit_stage"] == "Memory Injection"
