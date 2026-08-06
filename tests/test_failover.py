"""Phase 16: Endpoint Tagging & Failover tests.

Tests the failover chain behavior end-to-end: any failure (any HTTP status or
exception) on one endpoint advances to the next candidate. Only total exhaustion
returns an error (503). Each candidate can carry a different model/api_key.
"""
import pytest

from app.services.routing import FailoverEndpoint, RoutingResult

# ============================================================================
# Unit: RoutingResult / FailoverEndpoint dataclasses
# ============================================================================


def test_failover_endpoint_defaults():
    ep = FailoverEndpoint(base_url="http://x.test", api_key="sk-1")
    assert ep.api_base_path == ""
    assert ep.bypass_method == "none"
    assert ep.provider == ""
    assert ep.model == ""
    assert ep.endpoint_name == ""
    assert ep.priority == 1


def test_routing_result_defaults():
    rr = RoutingResult()
    assert rr.failover_chain == []
    assert rr.bypass_method == "none"


def test_routing_result_with_chain():
    c1 = FailoverEndpoint(base_url="http://a.test", endpoint_name="A", priority=1)
    c2 = FailoverEndpoint(base_url="http://b.test", endpoint_name="B", priority=2)
    rr = RoutingResult(base_url="http://a.test", failover_chain=[c1, c2])
    assert len(rr.failover_chain) == 2
    assert rr.failover_chain[0].endpoint_name == "A"
    assert rr.failover_chain[1].endpoint_name == "B"


# ============================================================================
# Integration: Proxy failover through the HTTP API
# ============================================================================


class TestProxyFailover:
    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        test_settings = Settings(
            default_endpoint_url="http://primary.test",
            default_endpoint_api_key="sk-primary",
            default_endpoint_model="test-model",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

    @pytest.mark.asyncio
    async def test_500_then_success(self, admin_client, httpx_mock):
        """A 500 on the primary endpoint advances to the secondary."""
        client, _, api_key = admin_client

        # Two endpoints tagged 'driver': primary (priority 1) and fallback (priority 2).
        ep1 = await client.post("/api/endpoints", json={
            "name": "Primary", "base_url": "http://primary.test", "api_key": "sk-1",
            "role_tag": "driver", "priority": 1,
        })
        await client.post("/api/endpoints", json={
            "name": "Fallback", "base_url": "http://fallback.test", "api_key": "sk-2",
            "role_tag": "driver", "priority": 2,
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep1.json()["id"]})

        httpx_mock.add_response(
            url="http://primary.test/v1/chat/completions",
            json={"error": {"message": "Internal error"}},
            status_code=500,
        )
        httpx_mock.add_response(
            url="http://fallback.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "Hello from fallback!"}}]},
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Hello from fallback!"

    @pytest.mark.asyncio
    async def test_401_then_success(self, admin_client, httpx_mock):
        """A 401 (bad key) advances — different endpoints can have different keys."""
        client, _, api_key = admin_client

        ep1 = await client.post("/api/endpoints", json={
            "name": "FreeTier", "base_url": "http://free.test", "api_key": "sk-bad",
            "role_tag": "driver", "priority": 1,
        })
        await client.post("/api/endpoints", json={
            "name": "PaidTier", "base_url": "http://paid.test", "api_key": "sk-good",
            "role_tag": "driver", "priority": 2,
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep1.json()["id"]})

        httpx_mock.add_response(
            url="http://free.test/v1/chat/completions",
            json={"error": {"message": "Unauthorized"}},
            status_code=401,
        )
        httpx_mock.add_response(
            url="http://paid.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "Paid response"}}]},
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Paid response"

    @pytest.mark.asyncio
    async def test_all_endpoints_exhausted_returns_503(self, admin_client, httpx_mock):
        """When all candidates fail, the client gets a 503."""
        client, _, api_key = admin_client

        ep1 = await client.post("/api/endpoints", json={
            "name": "EP1", "base_url": "http://ep1.test", "api_key": "sk-1",
            "role_tag": "driver", "priority": 1,
        })
        await client.post("/api/endpoints", json={
            "name": "EP2", "base_url": "http://ep2.test", "api_key": "sk-2",
            "role_tag": "driver", "priority": 2,
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep1.json()["id"]})

        httpx_mock.add_response(
            url="http://ep1.test/v1/chat/completions", status_code=500,
            json={"error": "down"},
        )
        httpx_mock.add_response(
            url="http://ep2.test/v1/chat/completions", status_code=503,
            json={"error": "also down"},
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_success_on_first_no_extra_calls(self, admin_client, httpx_mock):
        """When the primary succeeds, no failover occurs."""
        client, _, api_key = admin_client

        ep1 = await client.post("/api/endpoints", json={
            "name": "Primary", "base_url": "http://primary.test", "api_key": "sk-1",
            "role_tag": "driver", "priority": 1,
        })
        await client.post("/api/endpoints", json={
            "name": "Secondary", "base_url": "http://secondary.test", "api_key": "sk-2",
            "role_tag": "driver", "priority": 2,
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep1.json()["id"]})

        httpx_mock.add_response(
            url="http://primary.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "First try!"}}]},
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "First try!"
        # Only one upstream call was made.
        all_requests = httpx_mock.get_requests()
        assert len(all_requests) == 1

    @pytest.mark.asyncio
    async def test_single_endpoint_backward_compatible(self, admin_client, httpx_mock):
        """A single endpoint (no tag-mates) works identically to pre-failover."""
        client, _, api_key = admin_client

        ep = await client.post("/api/endpoints", json={
            "name": "Only", "base_url": "http://only.test", "api_key": "sk-only",
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep.json()["id"]})

        httpx_mock.add_response(
            url="http://only.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "Solo response"}}]},
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Solo response"


# ============================================================================
# Integration: Endpoint role_tag/priority fields via API
# ============================================================================


class TestEndpointTagFields:
    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token

    @pytest.mark.asyncio
    async def test_create_with_role_tag_and_priority(self, admin_client):
        client, _ = admin_client
        resp = await client.post("/api/endpoints", json={
            "name": "Driver", "base_url": "http://driver.test", "api_key": "sk",
            "role_tag": "driver", "priority": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["role_tag"] == "driver"
        assert data["priority"] == 3

    @pytest.mark.asyncio
    async def test_defaults_applied(self, admin_client):
        client, _ = admin_client
        resp = await client.post("/api/endpoints", json={
            "name": "Default", "base_url": "http://default.test", "api_key": "sk",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["role_tag"] == "default"
        assert data["priority"] == 1
        assert data["custom_tag"] == ""

    @pytest.mark.asyncio
    async def test_update_role_tag(self, admin_client):
        client, _ = admin_client
        create = await client.post("/api/endpoints", json={
            "name": "EP", "base_url": "http://ep.test", "api_key": "sk",
        })
        ep_id = create.json()["id"]
        resp = await client.put(f"/api/endpoints/{ep_id}", json={
            "role_tag": "navigator", "priority": 2,
        })
        assert resp.status_code == 200
        assert resp.json()["role_tag"] == "navigator"
        assert resp.json()["priority"] == 2


# ============================================================================
# Unit: _build_failover_chain selection rules
# ============================================================================

class TestFailoverChainConstruction:
    """Cover the chain query directly.

    TestProxyFailover exercises retry behaviour through the HTTP API, but every
    one of its scenarios uses endpoints that all belong to one user, are all
    enabled, and all share a role_tag -- so the query's filters never actually
    have to do anything. Mutation testing confirmed the gap: dropping the
    user_id, enabled and role_tag predicates, and reversing the ordering, all
    left the suite green. A cross-user leak here would route one user's traffic
    through another user's paid endpoint.
    """

    @staticmethod
    async def _chain(primary, user_id):
        from app.services.routing import _build_failover_chain
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as db:
            return await _build_failover_chain(db, primary, user_id)

    @staticmethod
    async def _make_user(username: str) -> str:
        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as db:
            user = User(username=username, password_hash="x", gitv_api_key=f"k-{username}")
            db.add(user)
            await db.commit()
            return user.id

    @staticmethod
    async def _make_endpoint(user_id: str, name: str, **kwargs):
        from app.models.endpoint import Endpoint
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as db:
            ep = Endpoint(
                user_id=user_id, name=name,
                base_url=kwargs.pop("base_url", f"https://{name}.test"),
                **kwargs,
            )
            db.add(ep)
            await db.commit()
            return ep

    async def test_primary_is_always_first(self):
        user_id = await self._make_user("chain_primary")
        primary = await self._make_endpoint(user_id, "primary", role_tag="driver")
        await self._make_endpoint(user_id, "mate", role_tag="driver")

        chain = await self._chain(primary, user_id)
        assert chain[0].endpoint_name == "primary"

    async def test_only_tag_mates_are_included(self):
        user_id = await self._make_user("chain_tags")
        primary = await self._make_endpoint(user_id, "driver-a", role_tag="driver")
        await self._make_endpoint(user_id, "driver-b", role_tag="driver")
        await self._make_endpoint(user_id, "verifier-a", role_tag="verifier")

        names = [c.endpoint_name for c in await self._chain(primary, user_id)]
        assert names == ["driver-a", "driver-b"]
        assert "verifier-a" not in names, "endpoint with a different role_tag entered the chain"

    async def test_disabled_endpoints_are_excluded(self):
        user_id = await self._make_user("chain_disabled")
        primary = await self._make_endpoint(user_id, "live", role_tag="driver")
        await self._make_endpoint(user_id, "switched-off", role_tag="driver", enabled=False)

        names = [c.endpoint_name for c in await self._chain(primary, user_id)]
        assert names == ["live"], "a disabled endpoint was offered as a failover target"

    async def test_other_users_endpoints_never_enter_the_chain(self):
        mine = await self._make_user("chain_mine")
        theirs = await self._make_user("chain_theirs")
        primary = await self._make_endpoint(mine, "mine-a", role_tag="driver")
        await self._make_endpoint(theirs, "theirs-a", role_tag="driver")

        names = [c.endpoint_name for c in await self._chain(primary, mine)]
        assert names == ["mine-a"], "another user's endpoint leaked into the failover chain"

    async def test_mates_are_ordered_by_priority(self):
        """Creation order is deliberately different from priority order.

        With only two mates, "oldest first" and "newest first" can accidentally
        agree with priority order and the assertion proves nothing. These three
        are seeded so that priority-ascending, created_at-ascending and
        created_at-descending all give different answers, leaving priority as
        the only ordering that produces the expected result.
        """
        from datetime import UTC, datetime

        user_id = await self._make_user("chain_priority")
        base = datetime(2026, 1, 1, tzinfo=UTC)
        primary = await self._make_endpoint(
            user_id, "p", role_tag="driver", priority=0, created_at=base
        )
        # created: alpha, beta, gamma   priority: beta(1), gamma(2), alpha(3)
        await self._make_endpoint(
            user_id, "alpha", role_tag="driver", priority=3, created_at=base.replace(day=2)
        )
        await self._make_endpoint(
            user_id, "beta", role_tag="driver", priority=1, created_at=base.replace(day=3)
        )
        await self._make_endpoint(
            user_id, "gamma", role_tag="driver", priority=2, created_at=base.replace(day=4)
        )

        names = [c.endpoint_name for c in await self._chain(primary, user_id)]
        assert names == ["p", "beta", "gamma", "alpha"], "failover order ignored priority"

    async def test_primary_is_not_duplicated_by_its_own_tag_query(self):
        user_id = await self._make_user("chain_dupe")
        primary = await self._make_endpoint(user_id, "solo", role_tag="driver")

        names = [c.endpoint_name for c in await self._chain(primary, user_id)]
        assert names == ["solo"]

    async def test_untagged_primary_uses_the_default_tag(self):
        user_id = await self._make_user("chain_default")
        primary = await self._make_endpoint(user_id, "plain")
        await self._make_endpoint(user_id, "also-plain")
        await self._make_endpoint(user_id, "tagged", role_tag="verifier")

        names = [c.endpoint_name for c in await self._chain(primary, user_id)]
        assert names == ["plain", "also-plain"]
