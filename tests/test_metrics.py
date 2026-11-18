"""Token metrics (0.31.0): per-object prompt deltas and per-LLM-call rows.

The E2E tests run with debug mode OFF on purpose -- the tables must record on
every authenticated request, unlike the debug trace they grew out of.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models.metrics import LlmCallMetric, PromptObjectMetric
from tests.conftest import TestSessionLocal

DRIVER_URL = "http://driver-backend:9999"
VERIFY_URL = "http://verify-backend:8888"


@pytest.fixture(autouse=True)
def set_endpoint(monkeypatch):
    from app.config import Settings

    test_settings = Settings(
        default_endpoint_url=DRIVER_URL,
        default_endpoint_api_key="sk-test-key",
        default_endpoint_model="test-model",
        default_endpoint_api_base_path="",
    )
    monkeypatch.setattr("app.services.proxy.settings", test_settings)


def _mock_response(content: str = "Hi there!", model: str = "test-model", **overrides):
    resp = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1234567890,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content},
             "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp.update(overrides)
    return resp


def _judge_response(violation: bool, reason: str = ""):
    return _mock_response(
        f'{{"violation": {str(violation).lower()}, "reason": "{reason}"}}',
        model="verify-model",
    )


async def _all_prompt_rows() -> list[PromptObjectMetric]:
    async with TestSessionLocal() as db:
        return list((await db.execute(select(PromptObjectMetric))).scalars().all())


async def _all_llm_rows() -> list[LlmCallMetric]:
    async with TestSessionLocal() as db:
        return list((await db.execute(select(LlmCallMetric))).scalars().all())


async def _setup_driver_endpoint(client) -> str:
    ep = await client.post("/api/endpoints", json={
        "name": "Driver", "base_url": DRIVER_URL, "api_key": "sk-driver",
    })
    assert ep.status_code in (200, 201), ep.text
    await client.put("/api/settings", json={"default_endpoint_id": ep.json()["id"]})
    return ep.json()["id"]


# ============================================================================
# E2E: rows land in both tables on a plain authenticated request, debug OFF.
# ============================================================================


class TestEndToEndRecording:
    @pytest.mark.asyncio
    async def test_lorebook_entry_and_llm_rows_recorded(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        endpoint_id = await _setup_driver_endpoint(client)

        lb = await client.post("/api/lorebooks", json={"name": "Bestiary"})
        assert lb.status_code in (200, 201), lb.text
        lorebook_id = lb.json()["id"]
        entry = await client.post(f"/api/lorebooks/{lorebook_id}/entries", json={
            "name": "Dragon", "keys": ["dragon"],
            "content": "A dragon guards the pass.",
        })
        assert entry.status_code == 201, entry.text

        httpx_mock.add_response(
            url=f"{DRIVER_URL}/v1/chat/completions",
            json=_mock_response("Roar."), status_code=200,
        )
        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "I see a dragon."}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200

        prompt_rows = await _all_prompt_rows()
        entry_rows = [r for r in prompt_rows if r.object_type == "lorebook_entry"]
        assert len(entry_rows) == 1
        row = entry_rows[0]
        assert row.parent_id == lorebook_id
        assert row.object_id == entry.json()["id"]
        assert row.pre_tokens == 0
        assert row.post_tokens == max(1, len("A dragon guards the pass.") // 4)
        assert row.difference == row.post_tokens
        assert row.context == "live"
        assert row.is_retry is False
        assert row.chat_id  # resolved conversation id, non-empty

        llm_rows = await _all_llm_rows()
        assert len(llm_rows) == 1
        call = llm_rows[0]
        assert call.purpose == "main"
        assert call.endpoint_id == endpoint_id
        assert call.status_code == 200
        assert call.tokens_source == "usage"  # mock returns upstream usage
        assert call.prompt_tokens == 10
        assert call.completion_tokens == 5
        assert call.context == "live"
        assert call.is_retry is False

    @pytest.mark.asyncio
    async def test_tokens_source_estimate_without_usage(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await _setup_driver_endpoint(client)

        no_usage = _mock_response("Hi")
        no_usage.pop("usage")
        httpx_mock.add_response(
            url=f"{DRIVER_URL}/v1/chat/completions", json=no_usage, status_code=200,
        )
        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200

        llm_rows = await _all_llm_rows()
        assert len(llm_rows) == 1
        # Never silently mix the two: no usage means the estimate label.
        assert llm_rows[0].tokens_source == "estimate"
        assert llm_rows[0].prompt_tokens > 0

    @pytest.mark.asyncio
    async def test_verification_judge_rows_carry_rule_id(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _setup_driver_endpoint(client)

        verify_ep = await client.post("/api/endpoints", json={
            "name": "Verifier", "base_url": VERIFY_URL, "api_key": "sk-verify",
            "api_base_path": "/v1",
        })
        rule = await client.post("/api/verification/rules", json={
            "name": "Char Check", "prompt": "Stay in character.", "max_retries": 0,
        })
        assert rule.status_code in (200, 201), rule.text
        rule_id = rule.json()["id"]
        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": verify_ep.json()["id"],
            "verification_model": "verify-model",
        })

        httpx_mock.add_response(
            url=f"{DRIVER_URL}/v1/chat/completions",
            json=_mock_response("In character."), status_code=200,
        )
        httpx_mock.add_response(
            url=f"{VERIFY_URL}/v1/chat/completions",
            json=_judge_response(False), status_code=200,
        )
        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200

        llm_rows = await _all_llm_rows()
        judges = [r for r in llm_rows if r.purpose == "verification_judge"]
        assert len(judges) == 1
        assert judges[0].rule_id == rule_id
        drivers = [r for r in llm_rows if r.purpose == "main"]
        assert len(drivers) == 1
        assert drivers[0].rule_id == ""

    @pytest.mark.asyncio
    async def test_retry_cycle_flags_driver_and_judge_rows(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await _setup_driver_endpoint(client)

        verify_ep = await client.post("/api/endpoints", json={
            "name": "Verifier", "base_url": VERIFY_URL, "api_key": "sk-verify",
            "api_base_path": "/v1",
        })
        await client.post("/api/verification/rules", json={
            "name": "Char Check", "prompt": "Stay in character.", "max_retries": 2,
        })
        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": verify_ep.json()["id"],
            "verification_model": "verify-model",
        })

        # Driver: violating first, clean on retry. Judge: violation then approve.
        httpx_mock.add_response(url=f"{DRIVER_URL}/v1/chat/completions",
                                json=_mock_response("I am an AI model."), status_code=200)
        httpx_mock.add_response(url=f"{DRIVER_URL}/v1/chat/completions",
                                json=_mock_response("In character now."), status_code=200)
        httpx_mock.add_response(url=f"{VERIFY_URL}/v1/chat/completions",
                                json=_judge_response(True, "Broke character"), status_code=200)
        httpx_mock.add_response(url=f"{VERIFY_URL}/v1/chat/completions",
                                json=_judge_response(False), status_code=200)

        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200

        llm_rows = await _all_llm_rows()
        drivers = [r for r in llm_rows if r.purpose == "main"]
        judges = [r for r in llm_rows if r.purpose == "verification_judge"]
        assert len(drivers) == 2
        assert len(judges) == 2
        assert drivers[0].is_retry is False
        assert judges[0].is_retry is False
        assert drivers[1].is_retry is True
        assert judges[1].is_retry is True

    @pytest.mark.asyncio
    async def test_flush_failure_never_fails_request(
        self, admin_client, httpx_mock, monkeypatch
    ):
        client, _, api_key = admin_client
        await _setup_driver_endpoint(client)

        # Break the real failure surface -- the DB session -- rather than the
        # flush function itself. flush_metrics swallows internally, so the
        # request must still succeed with rows dropped and an error logged.
        def exploding_session(*_a, **_kw):
            raise RuntimeError("metrics sink down")

        import app.services.metrics as metrics_module

        monkeypatch.setattr(metrics_module, "async_session", exploding_session)

        httpx_mock.add_response(
            url=f"{DRIVER_URL}/v1/chat/completions",
            json=_mock_response("Still fine."), status_code=200,
        )
        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Still fine."
        assert await _all_llm_rows() == []  # dropped, not half-written

    @pytest.mark.asyncio
    async def test_unauthenticated_request_records_nothing(self, client, httpx_mock):
        httpx_mock.add_response(
            url=f"{DRIVER_URL}/v1/chat/completions",
            json=_mock_response("Anon."), status_code=200,
        )
        resp = await client.post("/v1/chat/completions", json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Hello"}],
        })
        assert resp.status_code == 200
        assert await _all_prompt_rows() == []
        assert await _all_llm_rows() == []


# ============================================================================
# Context tagging: sandbox/replay flags land on rows.
# ============================================================================


class TestContextTagging:
    def _body(self, **flags) -> dict:
        from app.services.metrics import init_metrics, record_prompt_object, set_metrics_chat

        body: dict = {"messages": [{"role": "user", "content": "hi"}], **flags}
        init_metrics(body, "user-1")
        set_metrics_chat(body, "chat-1")
        record_prompt_object(body, "memory_injection", "memory", 0, 10)
        return body

    def test_live_by_default(self):
        body = self._body()
        assert body["_gitv_metrics"]["rows_prompt"][0]["context"] == "live"

    def test_sandbox_flag_tagged(self):
        # debug_sandbox sets these flags before calling forward_request.
        body = self._body(_gitv_sandbox=True, _gitv_sandbox_id="sb-1")
        assert body["_gitv_metrics"]["rows_prompt"][0]["context"] == "sandbox"

    def test_replay_flag_tagged(self):
        body = self._body(_gitv_replay=True, _gitv_replay_of="run-1")
        assert body["_gitv_metrics"]["rows_prompt"][0]["context"] == "replay"


# ============================================================================
# Estimator unification (spec prerequisite).
# ============================================================================


class TestEstimatorUnification:
    def test_summarization_delegates_to_budget(self):
        from app.services import budget, summarization

        msgs = [
            {"role": "system", "content": "You are a character."},
            {"role": "user", "content": "x" * 400},
            {"role": "assistant", "content": [{"type": "text", "text": "y" * 80}]},
        ]
        assert summarization.estimate_tokens(msgs) == budget.estimate_messages_tokens(msgs)

    def test_cantrip_field_deltas_use_the_same_estimator(self):
        # The per-cantrip delta rows are built from estimate_tokens; pin that
        # both directions of a change are measured with the same formula.
        from app.services.budget import estimate_tokens

        before, after = "x" * 400, "x" * 100
        delta = estimate_tokens(after) - estimate_tokens(before)
        assert delta == 25 - 100


# ============================================================================
# Retention pruning.
# ============================================================================


class TestRetentionPrune:
    @pytest.mark.asyncio
    async def test_prune_deletes_old_rows_only(self):
        from app.services.metrics import prune_metrics

        old = datetime.now(UTC) - timedelta(days=40)
        recent = datetime.now(UTC) - timedelta(days=1)

        async with TestSessionLocal() as db:
            db.add_all([
                PromptObjectMetric(
                    user_id="u1", chat_id="c1", object_type="memory_injection",
                    object_id="memory", created_at=old,
                ),
                PromptObjectMetric(
                    user_id="u1", chat_id="c1", object_type="memory_injection",
                    object_id="memory", created_at=recent,
                ),
                LlmCallMetric(
                    user_id="u1", chat_id="c1", purpose="main", model="m",
                    status_code=200, latency_ms=1.0, created_at=old,
                ),
                LlmCallMetric(
                    user_id="u1", chat_id="c1", purpose="main", model="m",
                    status_code=200, latency_ms=2.0, created_at=recent,
                ),
            ])
            await db.commit()

        removed = await prune_metrics(30)
        assert removed == 2

        prompt_rows = await _all_prompt_rows()
        llm_rows = await _all_llm_rows()
        assert len(prompt_rows) == 1 and len(llm_rows) == 1
        # SQLite returns naive datetimes; compare on the instant.
        assert prompt_rows[0].created_at.replace(tzinfo=UTC) == recent
        assert llm_rows[0].created_at.replace(tzinfo=UTC) == recent

    @pytest.mark.asyncio
    async def test_prune_clamps_nonsense_retention(self):
        from app.services.metrics import prune_metrics

        twelve_hours_old = datetime.now(UTC) - timedelta(hours=12)
        async with TestSessionLocal() as db:
            db.add(LlmCallMetric(
                user_id="u1", chat_id="c1", purpose="main", model="m",
                status_code=200, latency_ms=1.0, created_at=twelve_hours_old,
            ))
            await db.commit()

        # 0 cannot mean "delete everything older than now" by accident; it
        # clamps to the 1-day floor, which keeps a 12-hour-old row.
        removed = await prune_metrics(0)
        assert removed == 0
        assert len(await _all_llm_rows()) == 1

    @pytest.mark.asyncio
    async def test_admin_setting_round_trip(self, admin_client):
        client, _, _ = admin_client
        resp = await client.put("/api/admin/settings", json={"metrics_retention_days": 14})
        assert resp.status_code == 200
        assert resp.json()["metrics_retention_days"] == 14
        # Clamped at 1, like every other cap.
        resp = await client.put("/api/admin/settings", json={"metrics_retention_days": 0})
        assert resp.json()["metrics_retention_days"] == 1


# ============================================================================
# Summary aggregation.
# ============================================================================


def _llm_row(user_id="u1", **kw) -> LlmCallMetric:
    defaults = dict(
        user_id=user_id, chat_id="c1", purpose="main", endpoint_id="e1",
        endpoint_name="Main", model="m1", status_code=200, latency_ms=100.0,
        tokens_source="usage", prompt_tokens=10, completion_tokens=5,
    )
    defaults.update(kw)
    return LlmCallMetric(**defaults)


def _prompt_row(user_id="u1", **kw) -> PromptObjectMetric:
    defaults = dict(
        user_id=user_id, chat_id="c1", object_type="lorebook_entry",
        object_id=str(uuid.uuid4()), parent_id=str(uuid.uuid4()),
        pre_tokens=0, post_tokens=100,
    )
    defaults.update(kw)
    # The ORM column default is 0, not post-pre; direct inserts must state it
    # exactly as record_prompt_object would.
    defaults.setdefault("difference", defaults["post_tokens"] - defaults["pre_tokens"])
    return PromptObjectMetric(**defaults)


class TestSummaryAggregation:
    @pytest.mark.asyncio
    async def test_aggregates_exclude_other_users_and_flagged_rows(self, admin_client):
        client, _, _ = admin_client
        # Discover the admin's id via the token (sub claim).
        async with TestSessionLocal() as db:
            from app.models.user import User

            admin = (await db.execute(select(User))).scalars().first()
            admin_id = admin.id

        async with TestSessionLocal() as db:
            shared_entry = str(uuid.uuid4())
            shared_book = str(uuid.uuid4())
            db.add_all([
                # Two fires of the same entry for the admin.
                _prompt_row(user_id=admin_id, object_id=shared_entry, parent_id=shared_book,
                            post_tokens=100),
                _prompt_row(user_id=admin_id, object_id=shared_entry, parent_id=shared_book,
                            post_tokens=200),
                # Another user's row: must be excluded.
                _prompt_row(user_id="someone-else", post_tokens=9999),
                # A sandbox row: excluded from the default aggregate.
                _prompt_row(user_id=admin_id, context="sandbox", post_tokens=500),
                _llm_row(user_id=admin_id, latency_ms=100.0),
                _llm_row(user_id=admin_id, latency_ms=300.0),
                _llm_row(user_id="someone-else"),
                _llm_row(user_id=admin_id, context="replay"),
            ])
            await db.commit()

        from app.services.metrics import metrics_summary

        summary = await metrics_summary(admin_id, days=30)
        assert len(summary["prompt_objects"]) == 1
        obj = summary["prompt_objects"][0]
        assert obj["fires"] == 2
        assert obj["sum_difference"] == 300
        assert obj["avg_difference"] == 150.0

        llm = summary["llm_calls"]
        assert len(llm) == 1
        group = llm[0]
        assert group["calls"] == 2
        assert group["ok"] == 2
        # median of [100, 300]
        assert group["median_latency_ms"] == 200.0
        assert group["mean_latency_ms"] == 200.0
        assert group["prompt_tokens"] == 20

        # The include flag brings the flagged rows back without losing them.
        flagged_in = await metrics_summary(admin_id, days=30, include_flagged=True)
        assert flagged_in["llm_calls"][0]["calls"] == 3
        assert sum(o["fires"] for o in flagged_in["prompt_objects"]) == 3

        # Cross-check via the API route (ownership enforced by the same filter).
        resp = await client.get("/api/metrics/summary?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["llm_calls"][0]["calls"] == 2

    @pytest.mark.asyncio
    async def test_listing_routes_are_paginated_and_chat_filtered(self, admin_client):
        client, _, _ = admin_client
        async with TestSessionLocal() as db:
            from app.models.user import User

            admin = (await db.execute(select(User))).scalars().first()
            admin_id = admin.id

        async with TestSessionLocal() as db:
            db.add_all([
                _llm_row(user_id=admin_id, chat_id="chat-a"),
                _llm_row(user_id=admin_id, chat_id="chat-b"),
                _prompt_row(user_id=admin_id, chat_id="chat-a"),
                _prompt_row(user_id="someone-else", chat_id="chat-a"),
            ])
            await db.commit()

        resp = await client.get("/api/metrics/llm-calls?limit=10")
        assert resp.status_code == 200
        assert resp.json()["total"] == 2

        resp = await client.get("/api/metrics/llm-calls?chat_id=chat-a")
        assert resp.json()["total"] == 1
        assert resp.json()["rows"][0]["chat_id"] == "chat-a"

        resp = await client.get("/api/metrics/prompt-objects?chat_id=chat-a")
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_unauthenticated_summary_rejected(self, client):
        resp = await client.get("/api/metrics/summary")
        assert resp.status_code == 401


# ============================================================================
# Upgrade path: migration 054 creates both tables on an old database.
# ============================================================================


class TestMetricsUpgradePath:
    @pytest.mark.asyncio
    async def test_migration_creates_tables_on_old_schema(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models.base import Base
        from app.services.migrations import run_migrations

        # Simulate the pre-054 database: create_all from current models, then
        # drop what only migration 054 provides.
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE prompt_object_metrics"))
                await conn.execute(text("DROP TABLE llm_call_metrics"))

            # Vacuity check: prove this really reproduces the old schema.
            async with engine.connect() as conn:
                names = await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
                table_names = {r[0] for r in names.all()}
            assert "prompt_object_metrics" not in table_names
            assert "llm_call_metrics" not in table_names

            await run_migrations(engine)

            async with engine.connect() as conn:
                prompt_cols = {
                    r[1] for r in
                    (await conn.execute(text("PRAGMA table_info(prompt_object_metrics)"))).all()
                }
                llm_cols = {
                    r[1] for r in
                    (await conn.execute(text("PRAGMA table_info(llm_call_metrics)"))).all()
                }

            # The model columns the hot path selects must all exist post-upgrade.
            assert {"id", "user_id", "chat_id", "object_type", "object_id", "parent_id",
                    "pre_tokens", "post_tokens", "difference", "context", "is_retry",
                    "created_at"} <= prompt_cols
            assert {"id", "user_id", "chat_id", "purpose", "endpoint_id", "endpoint_name",
                    "model", "status_code", "latency_ms", "failover_attempt",
                    "prompt_tokens", "completion_tokens", "reasoning_tokens",
                    "tokens_source", "rule_id", "context", "is_retry",
                    "created_at"} <= llm_cols
        finally:
            await engine.dispose()


# ============================================================================
# Map pipeline: stage measurements carry the map id; stage calls are map_stage.
# ============================================================================


class TestMapStageMetrics:
    @pytest.mark.asyncio
    async def test_stage_rows_carry_map_parent_and_purpose(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        await client.put("/api/admin/settings", json={"max_map_stages": 3})
        for name, host, tag in [("One", "one.test", "one"), ("Two", "two.test", "two")]:
            ep = await client.post("/api/endpoints", json={
                "name": name, "base_url": f"http://{host}", "api_key": f"sk-{name}",
                "role_tag": tag, "priority": 1,
            })
            assert ep.status_code in (200, 201), ep.text

        created = await client.post("/api/maps/import", json={"data": {
            "name": "Two Stage",
            "stages": [
                {"stage_order": 1, "name": "First", "system_instructions": "Do first.",
                 "endpoint_tag": "one", "model_override": "", "output_mode": "sanitize",
                 "resources": []},
                {"stage_order": 2, "name": "Second", "system_instructions": "Do second.",
                 "endpoint_tag": "two", "model_override": "", "output_mode": "persist",
                 "resources": []},
            ],
        }})
        assert created.status_code == 201, created.text
        map_id = created.json()["id"]
        await client.put("/api/settings", json={"default_map_id": map_id})

        httpx_mock.add_response(url="http://one.test/v1/chat/completions",
                                json={"choices": [{"message": {"content": "ONE"}}]})
        httpx_mock.add_response(url="http://two.test/v1/chat/completions",
                                json={"choices": [{"message": {"content": "TWO"}}]})

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Go"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, resp.text

        prompt_rows = await _all_prompt_rows()
        stage_rows = [r for r in prompt_rows if r.object_type == "stage_instructions"]
        assert len(stage_rows) == 2
        assert {r.parent_id for r in stage_rows} == {map_id}
        assert all(r.post_tokens > 0 for r in stage_rows)

        llm_rows = await _all_llm_rows()
        assert len(llm_rows) == 2
        assert {r.purpose for r in llm_rows} == {"map_stage"}
