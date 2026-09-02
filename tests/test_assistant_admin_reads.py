"""Opt-in admin-read tools (sub-phase 26d).

`visible_tools` already hides this whole group unless the caller is an admin
*and* `assistant_admin_reads_enabled` is on; the visibility tests here prove
that end to end through the real registry rather than a synthetic tool.
"""

from __future__ import annotations

import json

import pytest

from app.main import app
from app.services.admin import get_admin_settings, update_admin_settings
from app.services.assistant.context import ToolContext
from app.services.assistant.executor import execute
from app.services.assistant.registry import TOOLS, Risk, visible_tools
from tests.conftest import test_engine


async def _ctx(client, token, *, is_admin=True, secrets=()):
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    admin = await get_admin_settings()
    return ToolContext(
        user_id=me.json()["id"],
        is_admin=is_admin,
        bearer_token=token,
        app=app,
        conversation_id="conv-admin-reads",
        admin=admin,
        secrets=secrets,
    )


async def _second_user(client):
    created = await client.post(
        "/api/users",
        json={"username": "notadmin", "password": "otherpass123", "is_admin": False},
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post(
        "/api/auth/login", json={"username": "notadmin", "password": "otherpass123"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], created.json()["id"]


def _ok(outcome):
    assert outcome.ok, outcome.result
    return outcome.result


def _find(results, substr):
    return [r for r in results if substr in r["name"]]


class TestRegistration:
    def test_every_admin_read_tool_is_admin_only_and_read(self):
        for name in ("schema_report", "read_server_logs", "install_health"):
            tool = TOOLS[name]
            assert tool.group == "admin_reads", name
            assert tool.admin_only is True, name
            assert tool.risk is Risk.READ, name
            assert tool.kind == "local", name


class TestVisibility:
    @pytest.mark.asyncio
    async def test_invisible_to_a_non_admin_even_with_the_flag_on(self, admin_client):
        client, _, _ = admin_client
        other_token, other_id = await _second_user(client)
        await update_admin_settings({"assistant_admin_reads_enabled": True})
        try:
            ctx = await _ctx(client, other_token, is_admin=False)
            names = {t.name for t in visible_tools(ctx)}
            assert "schema_report" not in names
            assert "read_server_logs" not in names
            assert "install_health" not in names
        finally:
            await update_admin_settings({"assistant_admin_reads_enabled": False})

    @pytest.mark.asyncio
    async def test_invisible_to_an_admin_with_the_flag_off(self, admin_client):
        client, token, _ = admin_client
        await update_admin_settings({"assistant_admin_reads_enabled": False})
        ctx = await _ctx(client, token, is_admin=True)
        names = {t.name for t in visible_tools(ctx)}
        assert "schema_report" not in names
        assert "read_server_logs" not in names
        assert "install_health" not in names

    @pytest.mark.asyncio
    async def test_visible_to_an_admin_with_the_flag_on(self, admin_client):
        client, token, _ = admin_client
        await update_admin_settings({"assistant_admin_reads_enabled": True})
        try:
            ctx = await _ctx(client, token, is_admin=True)
            names = {t.name for t in visible_tools(ctx)}
            assert {"schema_report", "read_server_logs", "install_health"} <= names
        finally:
            await update_admin_settings({"assistant_admin_reads_enabled": False})


class TestSchemaReport:
    @pytest.mark.asyncio
    async def test_no_drift_on_the_in_memory_database(self, admin_client, monkeypatch):
        """The conftest database is built straight from `Base.metadata`, so a
        diff against that same metadata must come back clean."""
        client, token, _ = admin_client
        monkeypatch.setattr("app.services.assistant.admin_reads.engine", test_engine)

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["schema_report"], {}))

        drift = _find(outcome, "schema drift")
        assert drift and drift[0]["passed"] is True

        migrations = _find(outcome, "applied migrations")
        assert migrations

    @pytest.mark.asyncio
    async def test_never_calls_run_schema_repair(self, admin_client, monkeypatch):
        client, token, _ = admin_client
        monkeypatch.setattr("app.services.assistant.admin_reads.engine", test_engine)

        called = {"ran": False}

        async def _spy(*args, **kwargs):
            called["ran"] = True
            return {}

        monkeypatch.setattr("app.services.schema_repair.run_schema_repair", _spy)

        ctx = await _ctx(client, token)
        _ok(await execute(ctx, TOOLS["schema_report"], {}))
        assert called["ran"] is False


class TestReadServerLogs:
    @pytest.mark.asyncio
    async def test_seeded_api_key_is_redacted(self, admin_client, monkeypatch):
        client, token, _ = admin_client
        secret = "sk-SERVER-LOG-SECRET"
        created = await client.post(
            "/api/endpoints", json={"name": "LogSecret", "base_url": "https://logsecret.test", "api_key": secret}
        )
        assert created.status_code == 201

        monkeypatch.setattr(
            "app.services.log_manager.read_recent_logs",
            lambda lines=200: [f"INFO forwarding request with key {secret}", "INFO another line"],
        )

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["read_server_logs"], {"lines": 50}))
        assert len(outcome) == 1
        assert secret not in outcome[0]["detail"]
        assert secret not in json.dumps(outcome)
        assert "another line" in outcome[0]["detail"]

    @pytest.mark.asyncio
    async def test_lines_argument_is_clamped(self, admin_client, monkeypatch):
        client, token, _ = admin_client
        seen = {}

        def _spy(lines=200):
            seen["lines"] = lines
            return ["one line"]

        monkeypatch.setattr("app.services.log_manager.read_recent_logs", _spy)

        ctx = await _ctx(client, token)
        _ok(await execute(ctx, TOOLS["read_server_logs"], {"lines": 5000}))
        assert seen["lines"] == 1000

        _ok(await execute(ctx, TOOLS["read_server_logs"], {"lines": -5}))
        assert seen["lines"] == 1


class TestInstallHealth:
    @pytest.mark.asyncio
    async def test_runs_without_raising_and_returns_one_per_category(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["install_health"], {}))

        names = {r["name"] for r in outcome}
        assert "signing key" in names
        assert "backup directory" in names
        assert "backup dump tool" in names
        assert "backup history" in names
        assert "SSL status" in names
        assert "certificate / LAN address match" in names
        assert ".env drift" in names
        assert "update chain" in names
        assert "self-reachability" in names
        # Every category produced exactly one entry, and the handler itself
        # never raised past its own boundary (an exception would appear as a
        # single failing "install_health" result instead of this spread).
        assert "install_health" not in names
