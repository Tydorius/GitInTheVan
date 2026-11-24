"""0.49.0 security fix: verification judge endpoints must belong to the user.

A judge call is sent with the named endpoint's stored API key. Before this
fix, none of the four paths handling a judge endpoint checked ownership, so
a user who knew another user's endpoint id could route judge traffic through
it at the owner's expense (reproduced via the real routes during the 0.48.1
release review). These tests cover both write paths (rule create/update and
the verification settings PUT) and the runtime lookups (global and per-rule),
including a foreign id already in the database, as pre-fix data would be.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.auth import generate_api_key, hash_password
from tests.conftest import TestSessionLocal

A_URL = "http://victim-backend:7777"
B_DRIVER_URL = "http://bob-backend:6666"
PATTERN = r"\bforbidden phrase\b"


def _llm(content: str) -> dict:
    return {
        "id": "r",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


class TestJudgeEndpointOwnership:
    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr(
            "app.services.proxy.settings",
            Settings(
                default_endpoint_url=B_DRIVER_URL,
                default_endpoint_api_key="sk-bob-driver",
                default_endpoint_model="test-model",
            ),
        )

    @staticmethod
    async def _make_second_user(username: str) -> tuple[str, str]:
        """Create a second user directly; returns (user_id, raw proxy key)."""
        from app.models.user import User

        raw_key, key_hash = generate_api_key()
        async with TestSessionLocal() as db:
            user = User(
                username=username, password_hash=hash_password("secondpass123"), gitv_api_key=key_hash
            )
            db.add(user)
            await db.commit()
            return user.id, raw_key

    @staticmethod
    async def _make_endpoint(user_id: str, name: str, base_url: str) -> str:
        from app.models.endpoint import Endpoint

        async with TestSessionLocal() as db:
            ep = Endpoint(user_id=user_id, name=name, base_url=base_url, api_key="sk-owner")
            db.add(ep)
            await db.commit()
            return ep.id

    @staticmethod
    @asynccontextmanager
    async def _second_client(username: str):
        """A logged-in AsyncClient for a freshly created second user, plus their proxy key."""
        from app.models.user import User

        raw_key, key_hash = generate_api_key()
        async with TestSessionLocal() as db:
            db.add(User(
                username=username, password_hash=hash_password("secondpass123"), gitv_api_key=key_hash
            ))
            await db.commit()

        transport = ASGITransport(app=app)
        ac = AsyncClient(transport=transport, base_url="http://test")
        try:
            login = await ac.post(
                "/api/auth/login", json={"username": username, "password": "secondpass123"}
            )
            assert login.status_code == 200, login.text
            ac.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
            yield ac, raw_key
        finally:
            await ac.aclose()

    @pytest.mark.asyncio
    async def test_rule_cannot_name_another_users_endpoint(self, admin_client):
        client_a, _, _ = admin_client
        a_ep = await client_a.post("/api/endpoints", json={
            "name": "A Judge", "base_url": A_URL, "api_key": "sk-A-secret",
        })
        a_ep_id = a_ep.json()["id"]

        async with self._second_client("bob_rule_create") as (b, _):
            resp = await b.post("/api/verification/rules", json={
                "name": "B rule", "prompt": "Judge it.", "verification_endpoint_id": a_ep_id,
            })
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"] == "Endpoint not found"

        from sqlalchemy import func, select

        from app.models.verification import VerificationRule

        async with TestSessionLocal() as db:
            count = (
                await db.execute(select(func.count()).select_from(VerificationRule).where(
                    VerificationRule.name == "B rule"))
            ).scalar_one()
            assert count == 0, "rule was created despite the foreign endpoint id"

    @pytest.mark.asyncio
    async def test_rule_update_cannot_name_another_users_endpoint(self, admin_client):
        client_a, _, _ = admin_client
        a_ep_id = (await client_a.post("/api/endpoints", json={
            "name": "A Judge", "base_url": A_URL, "api_key": "sk-A-secret",
        })).json()["id"]

        async with self._second_client("bob_rule_update") as (b, _):
            rule = await b.post("/api/verification/rules", json={"name": "B rule", "prompt": "Judge it."})
            assert rule.status_code == 201, rule.text
            rule_id = rule.json()["id"]

            resp = await b.put(f"/api/verification/rules/{rule_id}", json={
                "verification_endpoint_id": a_ep_id,
            })
            assert resp.status_code == 404, resp.text

            stored = (await b.get(f"/api/verification/rules/{rule_id}")).json()
            assert stored["verification_endpoint_id"] is None, "foreign id was persisted"

    @pytest.mark.asyncio
    async def test_global_settings_cannot_name_another_users_endpoint(self, admin_client):
        client_a, _, _ = admin_client
        a_ep_id = (await client_a.post("/api/endpoints", json={
            "name": "A Judge", "base_url": A_URL, "api_key": "sk-A-secret",
        })).json()["id"]

        async with self._second_client("bob_settings") as (b, _):
            resp = await b.put("/api/verification/settings", json={
                "verification_enabled": True, "verification_endpoint_id": a_ep_id,
            })
            assert resp.status_code == 404, resp.text

            stored = (await b.get("/api/verification/settings")).json()
            assert stored["verification_endpoint_id"] is None, "foreign id was persisted"

    @pytest.mark.asyncio
    async def test_own_endpoint_is_accepted_on_both_paths(self, admin_client):
        """Control: the fix must not lock a user out of their own endpoints."""
        async with self._second_client("bob_own_ep") as (b, _):
            own = await b.post("/api/endpoints", json={
                "name": "B Judge", "base_url": "http://bob-judge:5555", "api_key": "sk-B",
            })
            assert own.status_code == 201, own.text

            rule = await b.post("/api/verification/rules", json={
                "name": "B rule", "prompt": "Judge it.", "verification_endpoint_id": own.json()["id"],
            })
            assert rule.status_code == 201, rule.text
            assert rule.json()["verification_endpoint_id"] == own.json()["id"]

            settings = await b.put("/api/verification/settings", json={
                "verification_enabled": True, "verification_endpoint_id": own.json()["id"],
            })
            assert settings.status_code == 200, settings.text
            assert settings.json()["verification_endpoint_id"] == own.json()["id"]

    @pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
    @pytest.mark.asyncio
    async def test_stored_foreign_id_makes_no_cross_user_call(self, admin_client, httpx_mock):
        """Legacy data: a foreign id already stored (saved before this fix)
        must not resolve at runtime. The regex still runs; the judge never
        fires on the other user's endpoint; the reply is delivered."""
        from sqlalchemy import select

        from app.models.verification import VerificationRule

        client_a, _, _ = admin_client
        a_ep_id = (await client_a.post("/api/endpoints", json={
            "name": "A Judge", "base_url": A_URL, "api_key": "sk-A-secret",
        })).json()["id"]

        async with self._second_client("bob_legacy") as (b, raw_key):
            b_driver = await b.post("/api/endpoints", json={
                "name": "B Driver", "base_url": B_DRIVER_URL, "api_key": "sk-bob-driver",
            })
            await b.put("/api/settings", json={"default_endpoint_id": b_driver.json()["id"]})
            v = await b.put("/api/verification/settings", json={"verification_enabled": True})
            assert v.status_code == 200, v.text
            rule = await b.post("/api/verification/rules", json={
                "name": "B legacy rule", "prompt": "Judge it.", "regex_pattern": PATTERN,
            })
            assert rule.status_code == 201, rule.text
            # Simulate pre-fix data: point the rule at A's endpoint directly.
            async with TestSessionLocal() as db:
                row = (
                    await db.execute(
                        select(VerificationRule).where(VerificationRule.id == rule.json()["id"])
                    )
                ).scalar_one()
                row.verification_endpoint_id = a_ep_id
                await db.commit()

            httpx_mock.add_response(
                url=f"{B_DRIVER_URL}/v1/chat/completions", json=_llm("Bob's clean reply."), status_code=200,
            )
            # Registered only so a regression is observable as a REQUEST to the
            # victim's URL; the assertion below is that it is never consumed.
            httpx_mock.add_response(
                url=f"{A_URL}/v1/chat/completions",
                json=_llm('{"violation": false, "reason": "", "severity": "none"}'),
                status_code=200,
            )
            resp = await b.post("/v1/chat/completions", json={
                "model": "test", "messages": [{"role": "user", "content": "Hello."}],
            }, headers={"Authorization": f"Bearer {raw_key}"})

            assert resp.status_code == 200
            assert resp.json()["choices"][0]["message"]["content"] == "Bob's clean reply."
            urls = [str(r.url) for r in httpx_mock.get_requests()]
            assert A_URL not in urls, "a judge call was sent to another user's endpoint"
            assert urls == [f"{B_DRIVER_URL}/v1/chat/completions"], (
                "a call was made to an endpoint other than the driver: " + str(urls)
            )
