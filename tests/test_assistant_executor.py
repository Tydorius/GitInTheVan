"""The executor against the real app (Phase 26b).

Nothing is mocked here. The executor reaches the running FastAPI app through an
ASGI transport with the caller's own token, so these tests prove the properties
that matter: the assistant sees exactly what the route sees, another user's
content stays invisible, validation and content guards still fire, the
rate-limit skip needs the ASGI scope flag, and results are redacted and
truncated before they reach a transcript.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.main import app
from app.services.admin import get_admin_settings, update_admin_settings
from app.services.assistant.context import ToolContext
from app.services.assistant.executor import (
    AuthExpired,
    execute,
    read_current,
    redact_endpoints,
    redact_text,
)
from app.services.assistant.registry import TOOLS

VALID_CANTRIP = 'export default function(context) { return context }'


async def _ctx(client, token, *, secrets=(), is_admin=True):
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    admin = await get_admin_settings()
    return ToolContext(
        user_id=me.json()["id"],
        is_admin=is_admin,
        bearer_token=token,
        app=app,
        conversation_id="conv-exec",
        admin=admin,
        secrets=secrets,
    )


async def _second_user(client):
    """Create a second user and return an authenticated (headers, id) pair."""
    created = await client.post(
        "/api/users",
        json={"username": "other", "password": "otherpass123", "is_admin": False},
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post(
        "/api/auth/login", json={"username": "other", "password": "otherpass123"}
    )
    assert login.status_code == 200
    return login.json()["access_token"], created.json()["id"]


class TestReadsMatchTheRoute:
    @pytest.mark.asyncio
    async def test_list_cantrips_equals_the_direct_route(self, admin_client):
        client, token, _ = admin_client
        create = await client.post(
            "/api/cantrips", json={"name": "Alpha", "code": VALID_CANTRIP}
        )
        assert create.status_code == 201

        direct = await client.get("/api/cantrips")
        assert direct.status_code == 200

        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["list_cantrips"], {})
        assert outcome.ok
        assert outcome.status == 200
        assert outcome.result == direct.json()

    @pytest.mark.asyncio
    async def test_path_and_body_are_routed_correctly(self, admin_client):
        client, token, _ = admin_client
        create = await client.post(
            "/api/cantrips", json={"name": "Alpha", "code": VALID_CANTRIP}
        )
        cantrip_id = create.json()["id"]

        ctx = await _ctx(client, token)
        outcome = await execute(
            ctx, TOOLS["update_cantrip"], {"cantrip_id": cantrip_id, "name": "Renamed"}
        )
        assert outcome.ok, outcome.result
        reread = await client.get(f"/api/cantrips/{cantrip_id}")
        assert reread.json()["name"] == "Renamed"

    @pytest.mark.asyncio
    async def test_query_params_reach_the_route(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["list_debug_runs"], {"saved_only": True})
        assert outcome.ok, outcome.result

    @pytest.mark.asyncio
    async def test_traversal_in_a_path_value_is_refused(self, admin_client):
        """A path value must never be able to leave the tool's own route.

        `httpx.ASGITransport` builds the ASGI scope from the decoded path, so
        percent-encoding alone would not hold: `%2F` comes back as a separator
        before Starlette routes it. The executor rejects such values instead.
        """
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        for value in ("../../api/users", "a/b", "a%2Fb", "..", "a\\b"):
            outcome = await execute(ctx, TOOLS["get_cantrip"], {"cantrip_id": value})
            assert not outcome.ok, value
            assert outcome.status == 0, value
            assert "single path segment" in json.dumps(outcome.result), value

    @pytest.mark.asyncio
    async def test_an_ordinary_id_still_reaches_its_route(self, admin_client):
        """Vacuity guard for the rejection above."""
        client, token, _ = admin_client
        created = await client.post("/api/cantrips", json={"name": "Ok", "code": VALID_CANTRIP})
        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["get_cantrip"], {"cantrip_id": created.json()["id"]})
        assert outcome.ok
        assert outcome.result["name"] == "Ok"


class TestOwnership:
    @pytest.mark.asyncio
    async def test_second_users_cantrip_is_not_readable(self, admin_client):
        client, token, _ = admin_client
        other_token, _ = await _second_user(client)

        made = await client.post(
            "/api/cantrips",
            json={"name": "Theirs", "code": VALID_CANTRIP},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert made.status_code == 201
        their_id = made.json()["id"]

        # The admin's own list must exclude it, which needs a row that has to
        # be filtered out rather than an empty table.
        mine = await client.post("/api/cantrips", json={"name": "Mine", "code": VALID_CANTRIP})
        assert mine.status_code == 201

        ctx = await _ctx(client, token)
        listed = await execute(ctx, TOOLS["list_cantrips"], {})
        names = {c["name"] for c in listed.result["cantrips"]}
        assert names == {"Mine"}

        fetched = await execute(ctx, TOOLS["get_cantrip"], {"cantrip_id": their_id})
        assert not fetched.ok
        assert fetched.status in (403, 404)

    @pytest.mark.asyncio
    async def test_delete_across_users_is_refused(self, admin_client):
        client, token, _ = admin_client
        other_token, _ = await _second_user(client)
        made = await client.post(
            "/api/cantrips",
            json={"name": "Theirs", "code": VALID_CANTRIP},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        their_id = made.json()["id"]

        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["delete_cantrip"], {"cantrip_id": their_id})
        assert not outcome.ok
        assert outcome.status in (403, 404)

        still_there = await client.get(
            f"/api/cantrips/{their_id}", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert still_there.status_code == 200


class TestErrorsSurfaceAsResults:
    @pytest.mark.asyncio
    async def test_validation_error_becomes_a_tool_error(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["create_endpoint"], {"name": "No base url"})
        assert not outcome.ok
        assert outcome.status == 422
        assert "error" in outcome.result

    @pytest.mark.asyncio
    async def test_content_guard_fires_through_the_assistant_path(self, admin_client):
        client, token, _ = admin_client
        original = (await get_admin_settings()).max_script_size_kb
        try:
            await update_admin_settings({"max_script_size_kb": 1})
            ctx = await _ctx(client, token)
            outcome = await execute(
                ctx,
                TOOLS["create_cantrip"],
                {"name": "Huge", "code": "// " + ("x" * 4000)},
            )
            assert not outcome.ok
            assert outcome.status in (400, 413, 422)
        finally:
            await update_admin_settings({"max_script_size_kb": original})

    @pytest.mark.asyncio
    async def test_oversized_arguments_are_refused_without_dispatch(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = await execute(
            ctx, TOOLS["create_cantrip"], {"name": "x", "code": "y" * (33 * 1024)}
        )
        assert not outcome.ok
        assert outcome.status == 0
        assert "too large" in json.dumps(outcome.result)

    @pytest.mark.asyncio
    async def test_garbage_bearer_raises_auth_expired(self, admin_client):
        client, _, _ = admin_client
        admin = await get_admin_settings()
        ctx = ToolContext(
            user_id="whoever",
            is_admin=False,
            bearer_token="not-a-real-token",
            app=app,
            conversation_id="conv-exec",
            admin=admin,
        )
        with pytest.raises(AuthExpired):
            await execute(ctx, TOOLS["list_cantrips"], {})

    @pytest.mark.asyncio
    async def test_unknown_local_handler_is_an_error_not_a_crash(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        broken = replace(TOOLS["describe_tool"], name="probe_local", handler=None)
        outcome = await execute(ctx, broken, {})
        assert not outcome.ok


class TestRateLimitSkip:
    @pytest.mark.asyncio
    async def test_internal_scope_flag_skips_the_limiter(self, admin_client, monkeypatch):
        """The skip must depend on the ASGI flag, not on the path or a header."""
        client, token, _ = admin_client
        seen: list[str] = []

        async def spy(request):
            seen.append(request.url.path)

        ctx = await _ctx(client, token)
        monkeypatch.setattr("app.services.rate_limiter.check_api_rate_limit", spy)

        # An ordinary request goes through the limiter.
        await client.get("/api/cantrips")
        assert seen, "the limiter should see a normal browser request"
        before = len(seen)

        outcome = await execute(ctx, TOOLS["list_cantrips"], {})
        assert outcome.ok
        assert len(seen) == before, "the internal call must not reach the limiter"

    @pytest.mark.asyncio
    async def test_the_header_alone_does_not_skip_the_limiter(self, admin_client, monkeypatch):
        """`X-GITV-Assistant` is a marker, never the control."""
        client, _, _ = admin_client
        seen: list[str] = []

        async def spy(request):
            seen.append(request.url.path)

        monkeypatch.setattr("app.services.rate_limiter.check_api_rate_limit", spy)
        await client.get("/api/cantrips", headers={"X-GITV-Assistant": "1"})
        assert seen == ["/api/cantrips"]


class TestResultPipeline:
    @pytest.mark.asyncio
    async def test_truncation_kicks_in_at_the_admin_limit(self, admin_client):
        client, token, _ = admin_client
        for index in range(6):
            resp = await client.post(
                "/api/cantrips",
                json={"name": f"Cantrip {index}", "code": VALID_CANTRIP, "description": "z" * 400},
            )
            assert resp.status_code == 201

        original = (await get_admin_settings()).max_assistant_tool_result_kb
        try:
            await update_admin_settings({"max_assistant_tool_result_kb": 1})
            ctx = await _ctx(client, token)
            outcome = await execute(ctx, TOOLS["list_cantrips"], {})
            assert outcome.truncated
            assert outcome.result["truncated"] is True
            assert "narrow the query" in outcome.result["note"]
            assert len(outcome.result["partial"]) <= 1024
        finally:
            await update_admin_settings({"max_assistant_tool_result_kb": original})

    @pytest.mark.asyncio
    async def test_untruncated_results_come_back_as_objects(self, admin_client):
        client, token, _ = admin_client
        await client.post("/api/cantrips", json={"name": "Small", "code": VALID_CANTRIP})
        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["list_cantrips"], {})
        assert not outcome.truncated
        assert isinstance(outcome.result, dict)

    @pytest.mark.asyncio
    async def test_exact_secret_is_scrubbed_from_an_unrelated_tool(self, admin_client):
        client, token, _ = admin_client
        secret = "houseparty-token-9"
        resp = await client.post(
            "/api/cantrips",
            json={"name": "Leaky", "code": VALID_CANTRIP, "description": f"key {secret} here"},
        )
        assert resp.status_code == 201

        ctx = await _ctx(client, token, secrets=(secret,))
        outcome = await execute(ctx, TOOLS["list_cantrips"], {})
        assert secret not in json.dumps(outcome.result)
        assert "[redacted]" in json.dumps(outcome.result)

    @pytest.mark.asyncio
    async def test_read_current_returns_the_object_being_changed(self, admin_client):
        client, token, _ = admin_client
        created = await client.post(
            "/api/cantrips", json={"name": "Before", "code": VALID_CANTRIP}
        )
        cantrip_id = created.json()["id"]
        ctx = await _ctx(client, token)
        current = await read_current(
            ctx, TOOLS["update_cantrip"], {"cantrip_id": cantrip_id, "name": "After"}
        )
        assert current is not None
        assert current["name"] == "Before"

    @pytest.mark.asyncio
    async def test_read_current_is_none_when_the_object_is_gone(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        assert await read_current(ctx, TOOLS["update_cantrip"], {"cantrip_id": "nope"}) is None

    @pytest.mark.asyncio
    async def test_read_current_is_none_without_a_reader(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        assert await read_current(ctx, TOOLS["list_cantrips"], {}) is None


class TestRedactionUnits:
    def test_bearer_and_jwt_shapes(self):
        text = "Authorization: Bearer abc.def.ghi and eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig"
        out = redact_text(text)
        assert "abc.def.ghi" not in out
        assert "eyJhbGciOiJIUzI1NiJ9" not in out

    def test_key_prefixes(self):
        out = redact_text("sk-abcdef123456 and gitv_abcdef123456")
        assert "sk-abcdef123456" not in out
        assert "gitv_abcdef123456" not in out

    def test_key_value_shapes(self):
        out = redact_text("api_key=hunter2 password: swordfish token = abcdef")
        assert "hunter2" not in out
        assert "swordfish" not in out

    def test_json_survives_redaction(self):
        payload = json.dumps({"api_key": "sk-livekey123456", "name": "keep"})
        out = redact_text(payload)
        assert json.loads(out)["name"] == "keep"
        assert "sk-livekey123456" not in out

    def test_existing_markers_are_left_alone(self):
        out = redact_text(json.dumps({"api_key": "***"}))
        assert json.loads(out)["api_key"] == "***"

    def test_exact_secret_wins_over_shape(self):
        assert "wombat" not in redact_text("value wombat here", ("wombat",))

    def test_short_secrets_are_ignored(self):
        # A three-character "secret" would redact half the document.
        assert redact_text("a cat sat", ("cat",)) == "a cat sat"

    def test_empty_text(self):
        assert redact_text("") == ""


class TestEndpointProjection:
    def test_single_dict(self):
        out = redact_endpoints({"id": "1", "api_key": "sk-x"})
        assert out["api_key"] == "***"
        assert out["api_key_set"] is True

    def test_missing_key_reports_not_set(self):
        out = redact_endpoints({"id": "1", "api_key": ""})
        assert out["api_key_set"] is False

    def test_bare_list(self):
        out = redact_endpoints([{"api_key": "sk-x"}, {"api_key": ""}])
        assert [e["api_key_set"] for e in out] == [True, False]

    def test_list_response_envelope(self):
        out = redact_endpoints({"endpoints": [{"api_key": "sk-x"}], "total": 1})
        assert out["endpoints"][0]["api_key"] == "***"
        assert out["total"] == 1

    def test_non_endpoint_payload_passes_through(self):
        assert redact_endpoints({"cantrips": []}) == {"cantrips": []}
