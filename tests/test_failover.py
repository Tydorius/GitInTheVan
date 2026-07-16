"""Phase 16: Endpoint Tagging & Failover tests.

Tests the failover chain behavior end-to-end: any failure (any HTTP status or
exception) on one endpoint advances to the next candidate. Only total exhaustion
returns an error (503). Each candidate can carry a different model/api_key.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
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
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key
        client.headers.pop("Authorization", None)

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
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

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
