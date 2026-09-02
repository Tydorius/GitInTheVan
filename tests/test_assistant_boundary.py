"""The mechanical boundary around the assistant (Phase 26b).

The LLM is not a security control. These are the checks that are: a walk over
`app.routes` proving no registry tool sits on an admin-guarded route, that no
tool sits on a hard-denied prefix, that every registered route actually exists,
and that a live provider key never leaves through an endpoint read.

The vacuity check at the end is load-bearing. Without it the route walk passes
just as happily against an empty registry.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.routing import APIRoute

from app.dependencies import require_admin
from app.main import app
from app.services.assistant.executor import execute
from app.services.assistant.registry import (
    HARD_DENY_PREFIXES,
    HARD_DENY_ROUTES,
    TOOLS,
)

BOUNDARY_KEY = "sk-BOUNDARY-TEST"


def _dependant_calls(dependant, seen=None):
    """Every callable in a route's recursive dependency tree."""
    seen = seen if seen is not None else set()
    if id(dependant) in seen:
        return
    seen.add(id(dependant))
    if dependant.call is not None:
        yield dependant.call
    for sub in dependant.dependencies:
        yield from _dependant_calls(sub, seen)


def _admin_guarded_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if require_admin in set(_dependant_calls(route.dependant)):
            for method in route.methods or ():
                out.add((method, route.path))
    return out


def _all_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", None) or ():
            out.add((method, getattr(route, "path", "")))
    return out


def _registry_routes() -> set[tuple[str, str]]:
    return {(t.method, t.path) for t in TOOLS.values() if t.kind == "http"}


class TestRouteWalk:
    def test_the_walk_finds_admin_routes_at_all(self):
        # Vacuity guard for the guard: if this set were empty the next test
        # would pass against any registry whatsoever.
        guarded = _admin_guarded_routes()
        assert ("PUT", "/api/admin/settings") in guarded
        assert len(guarded) > 10

    def test_no_tool_sits_on_an_admin_guarded_route(self):
        overlap = _registry_routes() & _admin_guarded_routes()
        assert overlap == set(), f"assistant tools on admin-guarded routes: {sorted(overlap)}"

    def test_a_fake_admin_tool_makes_the_walk_fail(self):
        """Vacuity check: the assertion above must be able to fail."""
        probe = replace(
            TOOLS["get_settings"],
            name="probe_admin_settings",
            method="GET",
            path="/api/admin/settings",
        )
        TOOLS["probe_admin_settings"] = probe
        try:
            overlap = _registry_routes() & _admin_guarded_routes()
            assert overlap, "inserting an admin-route tool did not trip the walk"
        finally:
            TOOLS.pop("probe_admin_settings", None)
        assert _registry_routes() & _admin_guarded_routes() == set()

    def test_no_registry_path_starts_with_a_hard_deny_prefix(self):
        for method, path in _registry_routes():
            assert not path.startswith(HARD_DENY_PREFIXES), f"{method} {path}"

    def test_hard_denied_routes_are_absent(self):
        assert _registry_routes() & HARD_DENY_ROUTES == set()

    def test_every_http_tool_route_exists(self):
        missing = _registry_routes() - _all_routes()
        assert missing == set(), f"registry points at routes the app does not have: {sorted(missing)}"

    def test_assistant_own_routes_are_not_tools(self):
        for method, path in _registry_routes():
            assert not path.startswith("/api/assistant"), f"{method} {path}"


class TestKeyNeverLeaves:
    @pytest.mark.asyncio
    async def test_seeded_api_key_never_appears_in_endpoint_reads(self, admin_client):
        client, token, _ = admin_client

        create = await client.post(
            "/api/endpoints",
            json={"name": "Boundary", "base_url": "https://boundary.test", "api_key": BOUNDARY_KEY},
        )
        assert create.status_code == 201
        endpoint_id = create.json()["id"]

        ctx = await _ctx(client, token, secrets=(BOUNDARY_KEY,))
        listed = await execute(ctx, TOOLS["list_endpoints"], {})
        assert listed.ok
        assert BOUNDARY_KEY not in str(listed.result)
        rows = listed.result["endpoints"]
        assert rows[0]["api_key"] == "***"
        assert rows[0]["api_key_set"] is True

        single = await execute(ctx, TOOLS["get_endpoint"], {"endpoint_id": endpoint_id})
        assert single.ok
        assert BOUNDARY_KEY not in str(single.result)
        assert single.result["api_key"] == "***"
        assert single.result["api_key_set"] is True

    @pytest.mark.asyncio
    async def test_get_endpoint_does_not_leak_another_row(self, admin_client):
        """The projection narrows to one endpoint rather than returning the list."""
        client, token, _ = admin_client
        first = await client.post(
            "/api/endpoints",
            json={"name": "Alpha", "base_url": "https://a.test", "api_key": BOUNDARY_KEY},
        )
        second = await client.post(
            "/api/endpoints",
            json={"name": "Beta", "base_url": "https://b.test", "api_key": "sk-OTHER-KEY-VALUE"},
        )
        assert first.status_code == 201 and second.status_code == 201

        ctx = await _ctx(client, token, secrets=(BOUNDARY_KEY,))
        single = await execute(ctx, TOOLS["get_endpoint"], {"endpoint_id": second.json()["id"]})
        assert single.result["name"] == "Beta"
        assert "Alpha" not in str(single.result)

    @pytest.mark.asyncio
    async def test_the_projection_is_what_sets_the_marker(self, admin_client):
        """Mutation guard: drop the projection and `***` stops appearing."""
        client, token, _ = admin_client
        create = await client.post(
            "/api/endpoints",
            json={"name": "Boundary", "base_url": "https://boundary.test", "api_key": BOUNDARY_KEY},
        )
        assert create.status_code == 201

        # Secrets deliberately empty so only the projection and the shape-based
        # redaction are in play.
        ctx = await _ctx(client, token, secrets=())

        unprojected = replace(TOOLS["list_endpoints"], name="probe_raw", project_result=None)
        raw = await execute(ctx, unprojected, {})
        assert raw.result["endpoints"][0]["api_key"] != "***"
        # Shape-based redaction still catches an `sk-` key even unprojected.
        assert BOUNDARY_KEY not in str(raw.result)

        projected = await execute(ctx, TOOLS["list_endpoints"], {})
        assert projected.result["endpoints"][0]["api_key"] == "***"

    @pytest.mark.asyncio
    async def test_a_key_with_no_recognisable_shape_is_caught_by_the_projection(self, admin_client):
        """A provider key need not look like `sk-...`; the projection is the floor."""
        client, token, _ = admin_client
        odd_key = "plain-house-key-1234"
        create = await client.post(
            "/api/endpoints",
            json={"name": "Odd", "base_url": "https://odd.test", "api_key": odd_key},
        )
        assert create.status_code == 201

        ctx = await _ctx(client, token, secrets=())
        listed = await execute(ctx, TOOLS["list_endpoints"], {})
        assert odd_key not in str(listed.result)


async def _ctx(client, token, *, secrets):
    """A ToolContext for the authenticated caller."""
    from app.services.admin import get_admin_settings
    from app.services.assistant.context import ToolContext

    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    admin = await get_admin_settings()
    return ToolContext(
        user_id=me.json()["id"],
        is_admin=True,
        bearer_token=token,
        app=app,
        conversation_id="boundary",
        admin=admin,
        secrets=secrets,
    )
