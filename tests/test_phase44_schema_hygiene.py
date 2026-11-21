"""Phase 44 schema & 0.42.1/0.43.0 hygiene leftover tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.cantrip import Cantrip
from app.services.driver_callable import ToolCall, execute_tool_call
from app.services.jslorebook import _desanitize_content
from app.services.migrations import run_migrations

# ---------------------------------------------------------------------------
# 1. Upgrade-path migration tests (064 & 065)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upgrade_path_migrations_064_and_065(tmp_path):
    """Old-schema SQLite DB without the new columns upgrades cleanly."""
    db_path = tmp_path / "legacy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE maps (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    tag VARCHAR(128) NOT NULL DEFAULT '',
                    is_public BOOLEAN NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    version VARCHAR(32) NOT NULL DEFAULT '1.0',
                    author VARCHAR(128) NOT NULL DEFAULT '',
                    global_llm_instructions TEXT NOT NULL DEFAULT '',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE verification_rules (
                    id VARCHAR(36) PRIMARY KEY,
                    user_id VARCHAR(36) NOT NULL,
                    name VARCHAR(128) NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    prompt TEXT NOT NULL DEFAULT '',
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    tag VARCHAR(128) NOT NULL DEFAULT '',
                    max_retries INTEGER NOT NULL DEFAULT 2,
                    execution_order INTEGER NOT NULL DEFAULT 10,
                    resubmission_strategy VARCHAR(32) NOT NULL DEFAULT 'add_instructions',
                    verification_endpoint_id VARCHAR(36),
                    verification_model VARCHAR(128) NOT NULL DEFAULT '',
                    parameters_json TEXT NOT NULL DEFAULT '[]',
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO maps (id, user_id, name, created_at, updated_at)
                VALUES ('m1', 'u1', 'Old Map', '2026-01-01', '2026-01-01')
                """
            )
        )
        await conn.execute(
            text(
                """
                INSERT INTO verification_rules (id, user_id, name, created_at, updated_at)
                VALUES ('r1', 'u1', 'Old Rule', '2026-01-01', '2026-01-01')
                """
            )
        )

    # Vacuity check: prove the fixture really reproduces the pre-064/065
    # schema before migrating, so a later fixture drift cannot turn this
    # test into a no-op that passes for the wrong reason.
    async with engine.connect() as conn:
        map_cols = [
            row[1] for row in (await conn.execute(text("PRAGMA table_info(maps)"))).fetchall()
        ]
        rule_cols = [
            row[1]
            for row in (await conn.execute(text("PRAGMA table_info(verification_rules)"))).fetchall()
        ]
    assert "final_verification_enabled" not in map_cols
    assert "regex_pattern" not in rule_cols

    await run_migrations(engine)

    async with engine.connect() as conn:
        map_row = (
            await conn.execute(
                text("SELECT final_verification_enabled FROM maps WHERE id = 'm1'")
            )
        ).one()
        assert map_row[0] in (0, False)

        rule_row = (
            await conn.execute(
                text("SELECT regex_pattern FROM verification_rules WHERE id = 'r1'")
            )
        ).one()
        assert rule_row[0] == ""

    await engine.dispose()


# ---------------------------------------------------------------------------
# 2. Maps final_verification_enabled create/update/export/import round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_map_final_verification_round_trip(admin_client):
    http_client, token, _ = admin_client
    headers = {"Authorization": f"Bearer {token}"}

    # Default is False
    r1 = await http_client.post("/api/maps", json={"name": "Default Map"}, headers=headers)
    assert r1.status_code == 201, r1.text
    assert r1.json()["final_verification_enabled"] is False

    # Create with True
    r2 = await http_client.post(
        "/api/maps",
        json={"name": "Verified Map", "final_verification_enabled": True},
        headers=headers,
    )
    assert r2.status_code == 201, r2.text
    map_id = r2.json()["id"]
    assert r2.json()["final_verification_enabled"] is True

    # Export preserves True
    exp = await http_client.get(f"/api/maps/{map_id}/export", headers=headers)
    assert exp.status_code == 200, exp.text
    exported = exp.json()
    assert exported["final_verification_enabled"] is True

    # Import preserves True
    imp = await http_client.post(
        "/api/maps/import",
        json={"name": "Imported Verified", "data": exported},
        headers=headers,
    )
    assert imp.status_code == 201, imp.text
    assert imp.json()["final_verification_enabled"] is True

    # Update toggles back to False
    upd = await http_client.put(
        f"/api/maps/{map_id}",
        json={"final_verification_enabled": False},
        headers=headers,
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["final_verification_enabled"] is False

    # Old export without key defaults to False
    legacy_export = {k: v for k, v in exported.items() if k != "final_verification_enabled"}
    imp_legacy = await http_client.post(
        "/api/maps/import",
        json={"name": "Legacy Export", "data": legacy_export},
        headers=headers,
    )
    assert imp_legacy.status_code == 201, imp_legacy.text
    assert imp_legacy.json()["final_verification_enabled"] is False


# ---------------------------------------------------------------------------
# 3. VerificationRule regex_pattern create/update/export + validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verification_rule_regex_validation_and_round_trip(admin_client):
    http_client, token, _ = admin_client
    headers = {"Authorization": f"Bearer {token}"}

    # Invalid regex on create -> 422
    bad_create = await http_client.post(
        "/api/verification/rules",
        json={"name": "Bad Regex", "prompt": "check", "regex_pattern": "[unclosed("},
        headers=headers,
    )
    assert bad_create.status_code == 422, bad_create.text
    assert "Invalid regex pattern" in bad_create.json()["detail"]

    # Valid regex on create -> 201
    good_create = await http_client.post(
        "/api/verification/rules",
        json={
            "name": "Valid Regex",
            "prompt": "check",
            "regex_pattern": r"\bsecret-\d+\b",
        },
        headers=headers,
    )
    assert good_create.status_code == 201, good_create.text
    rule = good_create.json()
    rule_id = rule["id"]
    assert rule["regex_pattern"] == r"\bsecret-\d+\b"

    # Export includes regex_pattern
    exp = await http_client.get(f"/api/verification/rules/{rule_id}/export", headers=headers)
    assert exp.status_code == 200, exp.text
    assert exp.json()["regex_pattern"] == r"\bsecret-\d+\b"

    # Invalid regex on update -> 422
    bad_update = await http_client.put(
        f"/api/verification/rules/{rule_id}",
        json={"regex_pattern": "(?P<broken"},
        headers=headers,
    )
    assert bad_update.status_code == 422, bad_update.text

    # Valid regex update + clear to empty string
    good_update = await http_client.put(
        f"/api/verification/rules/{rule_id}",
        json={"regex_pattern": r"^foo.*bar$"},
        headers=headers,
    )
    assert good_update.status_code == 200, good_update.text
    assert good_update.json()["regex_pattern"] == r"^foo.*bar$"

    clear_update = await http_client.put(
        f"/api/verification/rules/{rule_id}",
        json={"regex_pattern": ""},
        headers=headers,
    )
    assert clear_update.status_code == 200, clear_update.text
    assert clear_update.json()["regex_pattern"] == ""


# ---------------------------------------------------------------------------
# 4. jslorebook _desanitize_content comment awareness
# ---------------------------------------------------------------------------


def test_jslorebook_desanitize_comments_with_apostrophe():
    # Typical upstream escaping: apostrophe is unescaped in `// don't` and `\n` is escaped
    raw = r'// don' + "'" + r't break here\nconst msg = "hello\nworld";\nreturn msg;'
    result = _desanitize_content(raw)
    assert result == '// don\'t break here\nconst msg = "hello\\nworld";\nreturn msg;'

    raw_block_comment = r'/* don' + "'" + r't flip quotes\nstill comment */\nlet x = 1;\n'
    block_result = _desanitize_content(raw_block_comment)
    assert block_result == "/* don't flip quotes\nstill comment */\nlet x = 1;"


# ---------------------------------------------------------------------------
# 5. driver_callable N6 user_data seeding skip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_callable_skips_user_data_when_unused():
    tool = Cantrip(
        id="c1",
        user_id="u1",
        name="no_user_data_tool",
        code='context.tool_result = "ok";',
        timeout_ms=1000,
    )
    call = ToolCall(name="no_user_data_tool", args={})

    fake_result = MagicMock(
        has_error=False,
        error="",
        chat_data={},
        chat_data_deletes=[],
        user_data={},
        user_data_deletes=[],
        cantrip_data={},
        cantrip_data_deletes=[],
        tool_result="ok",
        scenario="",
        personality="",
    )

    mock_session = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = False

    with (
        patch("app.services.driver_callable.async_session", return_value=mock_ctx),
        patch("app.services.cantrip._load_chat_data", new_callable=AsyncMock, return_value={}),
        patch("app.services.cantrip._load_user_data", new_callable=AsyncMock, return_value={"secret": "val"}) as mock_load_ud,
        patch("app.services.cantrip._load_cantrip_data", new_callable=AsyncMock, return_value={}),
        patch("app.services.cantrip._save_chat_data", new_callable=AsyncMock),
        patch("app.services.cantrip._save_user_data", new_callable=AsyncMock) as mock_save_ud,
        patch("app.services.cantrip._save_cantrip_data", new_callable=AsyncMock),
        patch("app.services.deno_runner.run_cantrip", new_callable=AsyncMock, return_value=fake_result) as mock_run,
    ):
        out = await execute_tool_call(
            tool=tool,
            call=call,
            messages=[{"role": "user", "content": "hi"}],
            request_headers={},
            user_id="u1",
            conversation_id="conv-1",
            persist_shared=True,
        )

    assert out == "ok"
    mock_load_ud.assert_not_awaited()
    mock_save_ud.assert_not_awaited()
    assert mock_run.call_args.kwargs["user_data"] == {}


@pytest.mark.asyncio
async def test_driver_callable_propagates_unnamed_user_data_writes():
    """Same safety net as cantrip.py (0.43.0): a tool whose code reaches the
    store without the literal name (introspection, a runtime-built key) must
    still have its writes persisted -- before this release the driver-callable
    path saved unconditionally, so dropping unnamed writes is a regression."""
    tool = Cantrip(
        id="c2",
        user_id="u1",
        name="dynamic_ud_tool",
        code="const k = 'user' + '_data'; context.tool_result = 'ok';",
        timeout_ms=1000,
    )
    call = ToolCall(name="dynamic_ud_tool", args={})

    fake_result = MagicMock(
        has_error=False,
        error="",
        chat_data={},
        chat_data_deletes=[],
        user_data={"dyn": 1},
        user_data_deletes=["old"],
        cantrip_data={},
        cantrip_data_deletes=[],
        tool_result="ok",
        scenario="",
        personality="",
    )

    mock_session = AsyncMock()
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = False

    with (
        patch("app.services.driver_callable.async_session", return_value=mock_ctx),
        patch("app.services.cantrip._load_chat_data", new_callable=AsyncMock, return_value={}),
        patch("app.services.cantrip._load_user_data", new_callable=AsyncMock),
        patch("app.services.cantrip._load_cantrip_data", new_callable=AsyncMock, return_value={}),
        patch("app.services.cantrip._save_chat_data", new_callable=AsyncMock),
        patch("app.services.cantrip._save_user_data", new_callable=AsyncMock) as mock_save_ud,
        patch("app.services.cantrip._save_cantrip_data", new_callable=AsyncMock),
        patch("app.services.deno_runner.run_cantrip", new_callable=AsyncMock, return_value=fake_result),
    ):
        out = await execute_tool_call(
            tool=tool,
            call=call,
            messages=[{"role": "user", "content": "hi"}],
            request_headers={},
            user_id="u1",
            conversation_id="conv-1",
            persist_shared=True,
        )

    assert out == "ok"
    mock_save_ud.assert_awaited_once()
    assert mock_save_ud.call_args.args[2] == {"dyn": 1}  # positional store dict
    assert mock_save_ud.call_args.kwargs["deletes"] == {"old"}


def test_regex_pattern_length_capped():
    """The pre-check pattern runs against every full reply; a pathological
    multi-KB pattern is bounded to MAX_REGEX_PATTERN_CHARS."""
    from fastapi import HTTPException

    from app.routers.verification import MAX_REGEX_PATTERN_CHARS, _validate_regex_pattern

    _validate_regex_pattern("a" * MAX_REGEX_PATTERN_CHARS, 50000)  # at the cap
    with pytest.raises(HTTPException) as exc_info:
        _validate_regex_pattern("a" * (MAX_REGEX_PATTERN_CHARS + 1), 50000)
    assert exc_info.value.status_code == 422
