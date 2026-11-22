"""Phase 46 (0.46.0) schema, migrations, cached_tokens metrics, history_window, and summarization_block_size tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base
from app.models.metrics import LlmCallMetric
from app.models.user import User
from app.routers.debug import DebugRunTotals
from app.services.debug_metrics import (
    PURPOSE_MAIN,
    extract_usage,
    record_llm_call,
    summarize_run,
)
from app.services.metrics import flush_metrics, init_metrics
from app.services.migrations import run_migrations
from tests.conftest import TestSessionLocal


class TestCachedTokensExtraction:
    def test_openai_gemini_prompt_tokens_details(self):
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
                "prompt_tokens_details": {"cached_tokens": 750},
            }
        })
        assert usage is not None
        assert usage["cached_tokens"] == 750
        assert usage["prompt_tokens"] == 1000
        assert usage["completion_tokens"] == 200

    def test_anthropic_cache_read_input_tokens(self):
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 800,
                "completion_tokens": 100,
                "cache_read_input_tokens": 600,
            }
        })
        assert usage is not None
        assert usage["cached_tokens"] == 600

    def test_flat_cached_tokens_fallback(self):
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "cached_tokens": 320,
            }
        })
        assert usage is not None
        assert usage["cached_tokens"] == 320

    def test_priority_order_prefers_prompt_tokens_details(self):
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 400},
                "cache_read_input_tokens": 300,
                "cached_tokens": 200,
            }
        })
        assert usage is not None
        assert usage["cached_tokens"] == 400

    def test_zero_cached_tokens_preserved_as_zero(self):
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 500,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cached_tokens": 0},
            }
        })
        assert usage is not None
        assert usage["cached_tokens"] == 0

    def test_missing_and_invalid_cached_tokens_return_none(self):
        # Missing altogether
        usage = extract_usage({"usage": {"prompt_tokens": 100, "completion_tokens": 10}})
        assert usage is not None
        assert usage["cached_tokens"] is None

        # Bools, negatives, garbage strings, dicts
        for bad in (True, False, -5, "not-a-number", {"nested": 1}, [1]):
            usage = extract_usage({
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": bad},
                    "cache_read_input_tokens": bad,
                    "cached_tokens": bad,
                }
            })
            assert usage is not None
            assert usage["cached_tokens"] is None, f"Expected None for {bad!r}"

    def test_summarize_run_and_debug_totals_aggregate_cached_tokens(self):
        trace = {
            "run": {
                "llm_calls": [
                    {
                        "prompt_tokens": 1000,
                        "completion_tokens": 100,
                        "reasoning_tokens": 20,
                        "cached_tokens": 600,
                        "latency_ms": 150.0,
                        "tokens_source": "upstream",
                    },
                    {
                        "prompt_tokens": 500,
                        "completion_tokens": 50,
                        "reasoning_tokens": 0,
                        "cached_tokens": None,
                        "latency_ms": 100.0,
                        "tokens_source": "upstream",
                    },
                    {
                        "prompt_tokens": 300,
                        "completion_tokens": 30,
                        "reasoning_tokens": 0,
                        "cached_tokens": 150,
                        "latency_ms": 50.0,
                        "tokens_source": "upstream",
                    },
                ]
            }
        }
        totals = summarize_run(trace, total_latency_ms=350.0)
        assert totals["cached_tokens"] == 750
        parsed = DebugRunTotals(**totals)
        assert parsed.cached_tokens == 750
        # Backward compatibility: older trace totals without cached_tokens
        legacy = DebugRunTotals(prompt_tokens=100, completion_tokens=20)
        assert legacy.cached_tokens == 0


class TestCachedTokensPersistenceAndApi:
    @pytest.mark.asyncio
    async def test_persist_and_aggregate_with_null_backward_compat(self, admin_client):
        client, _, _ = admin_client
        async with TestSessionLocal() as db:
            user = (await db.execute(select(User).where(User.username == "admin"))).scalar_one()

        body: dict = {"_gitv_debug": {"run": {"llm_calls": [], "cantrips": [], "totals": {}}}}
        init_metrics(body, user.id)

        # Call 1: OpenAI cached_tokens = 450
        record_llm_call(
            body,
            purpose=PURPOSE_MAIN,
            latency_ms=120.0,
            status_code=200,
            response_data={
                "model": "gpt-4o",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "prompt_tokens_details": {"cached_tokens": 450},
                },
            },
            endpoint_id="ep-1",
            endpoint_name="Primary",
            model_requested="gpt-4o",
            model_resolved="gpt-4o",
        )
        # Call 2: No cached_tokens key -> stored as NULL (None)
        record_llm_call(
            body,
            purpose=PURPOSE_MAIN,
            latency_ms=80.0,
            status_code=200,
            response_data={
                "model": "gpt-4o",
                "usage": {
                    "prompt_tokens": 500,
                    "completion_tokens": 50,
                },
            },
            endpoint_id="ep-1",
            endpoint_name="Primary",
            model_requested="gpt-4o",
            model_resolved="gpt-4o",
        )
        # Call 3: Anthropic cache_read_input_tokens = 250
        record_llm_call(
            body,
            purpose=PURPOSE_MAIN,
            latency_ms=90.0,
            status_code=200,
            response_data={
                "model": "gpt-4o",
                "usage": {
                    "prompt_tokens": 600,
                    "completion_tokens": 60,
                    "cache_read_input_tokens": 250,
                },
            },
            endpoint_id="ep-1",
            endpoint_name="Primary",
            model_requested="gpt-4o",
            model_resolved="gpt-4o",
        )

        # Debug trace recorded cached_tokens per call
        trace_calls = body["_gitv_debug"]["run"]["llm_calls"]
        assert [c["cached_tokens"] for c in trace_calls] == [450, None, 250]

        await flush_metrics(body)

        # Verify DB rows directly
        async with TestSessionLocal() as db:
            rows = (await db.execute(select(LlmCallMetric))).scalars().all()
            assert len(rows) == 3
            assert sorted(r.cached_tokens for r in rows if r.cached_tokens is not None) == [250, 450]
            assert sum(1 for r in rows if r.cached_tokens is None) == 1

        # Verify /api/metrics/summary aggregates cached_tokens (NULL treated as 0)
        summary_resp = await client.get("/api/metrics/summary")
        assert summary_resp.status_code == 200, summary_resp.text
        summary = summary_resp.json()
        assert len(summary["llm_calls"]) == 1
        agg = summary["llm_calls"][0]
        assert agg["prompt_tokens"] == 2100
        assert agg["completion_tokens"] == 210
        assert agg["cached_tokens"] == 700

        # Verify /api/metrics/llm-calls preserves None on absent rows and int on present rows
        rows_resp = await client.get("/api/metrics/llm-calls")
        assert rows_resp.status_code == 200, rows_resp.text
        api_rows = rows_resp.json()["rows"]
        assert len(api_rows) == 3
        values = [r["cached_tokens"] for r in api_rows]
        assert None in values
        assert 450 in values
        assert 250 in values


class TestMapStageHistoryWindow:
    @pytest.mark.asyncio
    async def test_crud_and_export_import_roundtrip(self, admin_client):
        client, _, _ = admin_client

        # Create map with history_window on stage
        create_resp = await client.post("/api/maps", json={
            "name": "Windowed Map",
            "stages": [
                {"name": "Stage 1", "history_window": 6},
                {"name": "Stage 2"},
            ],
        })
        assert create_resp.status_code == 201, create_resp.text
        created = create_resp.json()
        map_id = created["id"]
        assert created["stages"][0]["history_window"] == 6
        assert created["stages"][1]["history_window"] == 0

        # Update history_window
        update_resp = await client.put(f"/api/maps/{map_id}", json={
            "stages": [
                {"name": "Stage 1", "history_window": 12},
                {"name": "Stage 2", "history_window": 4},
            ],
        })
        assert update_resp.status_code == 200, update_resp.text
        updated = update_resp.json()
        assert [s["history_window"] for s in updated["stages"]] == [12, 4]

        # Negative history_window rejected with 422
        bad_resp = await client.post("/api/maps", json={
            "name": "Bad Map",
            "stages": [{"name": "Stage Bad", "history_window": -1}],
        })
        assert bad_resp.status_code == 422

        # Export includes history_window
        export_resp = await client.get(f"/api/maps/{map_id}/export")
        assert export_resp.status_code == 200, export_resp.text
        exported = export_resp.json()
        assert [s["history_window"] for s in exported["stages"]] == [12, 4]

        # Import restores history_window
        import_resp = await client.post("/api/maps/import", json={
            "name": "Reimported Map",
            "data": exported,
        })
        assert import_resp.status_code == 201, import_resp.text
        reimported = import_resp.json()
        assert [s["history_window"] for s in reimported["stages"]] == [12, 4]

        # Import older pack missing history_window (and invalid/bool values) defaults to 0
        legacy_pack = {
            "name": "Legacy Pack",
            "stages": [
                {"stage_order": 1, "name": "Legacy Stage"},
                {"stage_order": 2, "name": "Bad Value Stage", "history_window": "nope"},
                {"stage_order": 3, "name": "Bool Stage", "history_window": True},
            ],
        }
        legacy_resp = await client.post("/api/maps/import", json={"data": legacy_pack})
        assert legacy_resp.status_code == 201, legacy_resp.text
        assert [s["history_window"] for s in legacy_resp.json()["stages"]] == [0, 0, 0]


class TestSummarizationBlockSizeSetting:
    @pytest.mark.asyncio
    async def test_get_put_roundtrip_and_negative_rejection(self, admin_client):
        client, _, _ = admin_client

        # Default is 0
        get_resp = await client.get("/api/settings")
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["summarization_block_size"] == 0

        # Update to 8
        put_resp = await client.put("/api/settings", json={"summarization_block_size": 8})
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["summarization_block_size"] == 8

        # Persists on subsequent GET
        get_resp2 = await client.get("/api/settings")
        assert get_resp2.status_code == 200
        assert get_resp2.json()["summarization_block_size"] == 8

        # Reset to 0
        put_zero = await client.put("/api/settings", json={"summarization_block_size": 0})
        assert put_zero.status_code == 200
        assert put_zero.json()["summarization_block_size"] == 0

        # Negative rejected with 422
        bad_resp = await client.put("/api/settings", json={"summarization_block_size": -3})
        assert bad_resp.status_code == 422

        # Dedicated /api/summarization/settings endpoint also roundtrips and rejects negatives
        sum_get = await client.get("/api/summarization/settings")
        assert sum_get.status_code == 200, sum_get.text
        assert sum_get.json()["summarization_block_size"] == 0

        sum_put = await client.put("/api/summarization/settings", json={"summarization_block_size": 6})
        assert sum_put.status_code == 200, sum_put.text
        assert sum_put.json()["summarization_block_size"] == 6

        sum_bad = await client.put("/api/summarization/settings", json={"summarization_block_size": -1})
        assert sum_bad.status_code == 422


class TestPhase46MigrationsUpgradePath:
    @pytest.mark.asyncio
    async def test_068_069_070_arrive_on_upgraded_database(self):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(text("ALTER TABLE llm_call_metrics DROP COLUMN cached_tokens;"))
            await conn.execute(text("ALTER TABLE map_stages DROP COLUMN history_window;"))
            await conn.execute(text("ALTER TABLE user_settings DROP COLUMN summarization_block_size;"))

        await run_migrations(engine)

        async with engine.begin() as conn:
            for table, col in (
                ("llm_call_metrics", "cached_tokens"),
                ("map_stages", "history_window"),
                ("user_settings", "summarization_block_size"),
            ):
                result = await conn.execute(text(f"PRAGMA table_info({table});"))
                cols = {row[1] for row in result.fetchall()}
                assert col in cols, f"{table}.{col} missing after migration"

        await engine.dispose()
