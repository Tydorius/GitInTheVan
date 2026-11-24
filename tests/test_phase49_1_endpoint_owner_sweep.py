"""0.49.1 security sweep: every user-supplied endpoint id is owner-checked.

Project rule (maintainer directive 2026-11-24): no user data crosses user
boundaries — the only sanctioned cross-user channel is installing content
packs. This file covers the paths the 0.49.1 sweep found unchecked beyond
the verification judges closed in 0.49.0:

- ``user_settings.summarization_endpoint_id`` — write was raw AND the read
  was unscoped, so stored foreign ids resolved: summarizer calls (and the
  lexicon endpoint chain, which lists the summarizer id as a candidate)
  went out with another user's API key.
- api-key create, map stage endpoint pins, scenario rule endpoints —
  writes were raw; reads were already user-scoped, so nothing leaked at
  runtime, but foreign ids were storable.

Every test here was run against the pre-fix code and failed (write tests
got 2xx; read tests resolved the foreign endpoint).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.main import app
from app.services.auth import generate_api_key, hash_password
from tests.conftest import TestSessionLocal

A_URL = "http://victim-backend:7777"


class TestEndpointOwnerSweep:
    @staticmethod
    @asynccontextmanager
    async def _second_client(username: str):
        """A logged-in AsyncClient for a freshly created second user."""
        from app.models.user import User

        raw_key, key_hash = generate_api_key()
        async with TestSessionLocal() as db:
            db.add(User(
                username=username, password_hash=hash_password("secondpass123"),
                gitv_api_key=key_hash,
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
            yield ac
        finally:
            await ac.aclose()

    @staticmethod
    async def _victims_endpoint_id(client_a) -> str:
        resp = await client_a.post("/api/endpoints", json={
            "name": "A endpoint", "base_url": A_URL, "api_key": "sk-A-secret",
        })
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    # -- summarization: the live leak (write + read + lexicon chain) --------

    @pytest.mark.asyncio
    async def test_summarization_endpoint_write_rejects_foreign_id(self, admin_client):
        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_summ_write") as b:
            resp = await b.put("/api/summarization/settings", json={
                "summarization_endpoint_id": a_ep,
            })
            assert resp.status_code == 404, resp.text
            stored = (await b.get("/api/summarization/settings")).json()
            assert stored["summarization_endpoint_id"] is None, "foreign id persisted"

    @pytest.mark.asyncio
    async def test_stored_foreign_summarization_id_resolves_to_none(self, admin_client):
        """Legacy data: a foreign id saved before the fix must not resolve."""
        from app.models.user_settings import UserSettings
        from app.services.summarization import load_summarization_config

        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_summ_read") as b:
            own = await b.post("/api/endpoints", json={
                "name": "B endpoint", "base_url": "http://bob-endpoint:3333", "api_key": "sk-B",
            })
            s = await b.put("/api/summarization/settings", json={
                "summarization_enabled": True,
                "summarization_endpoint_id": own.json()["id"],
            })
            assert s.status_code == 200, s.text
            # Simulate pre-fix data: repoint the stored id at A's endpoint.
            async with TestSessionLocal() as db:
                row = (
                    await db.execute(select(UserSettings).where(
                        UserSettings.summarization_endpoint_id == own.json()["id"]))
                ).scalar_one()
                row.summarization_endpoint_id = a_ep
                await db.commit()
                owner = row.user_id

            async with TestSessionLocal() as db:
                settings, endpoint = await load_summarization_config(db, owner)
            assert settings is not None
            assert endpoint is None, "a foreign summarization endpoint resolved at runtime"

    @pytest.mark.asyncio
    async def test_lexicon_chain_never_includes_foreign_endpoints(self, admin_client):
        from app.models.user_settings import UserSettings
        from app.services.lexicon import load_lexicon_endpoints

        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_lex") as b:
            own = await b.post("/api/endpoints", json={
                "name": "B endpoint", "base_url": "http://bob-endpoint:1111", "api_key": "sk-B",
            })
            await b.put("/api/settings", json={"default_endpoint_id": own.json()["id"]})
            # Direct-DB foreign summarization id: lexicon lists it as candidate 2.
            async with TestSessionLocal() as db:
                row = (
                    await db.execute(select(UserSettings).where(
                        UserSettings.default_endpoint_id == own.json()["id"]))
                ).scalar_one()
                row.summarization_endpoint_id = a_ep
                await db.commit()
                owner = row.user_id

            async with TestSessionLocal() as db:
                _, chain = await load_lexicon_endpoints(db, owner)
            urls = [e.base_url for e in chain]
            assert A_URL not in urls, "the lexicon chain included another user's endpoint"
            assert urls, "expected the user's own default endpoint in the chain"

    # -- hygiene 404s: writes that previously stored foreign ids raw --------

    @pytest.mark.asyncio
    async def test_api_key_create_rejects_foreign_endpoint(self, admin_client):
        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_key") as b:
            resp = await b.post("/api/api-keys", json={
                "label": "pinned", "endpoint_id": a_ep,
            })
            assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_map_stage_pins_reject_foreign_endpoints(self, admin_client):
        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_map") as b:
            resp = await b.post("/api/maps", json={
                "name": "M", "tag": "", "stages": [{
                    "name": "S1", "system_instructions": "x", "endpoint_id": a_ep,
                }],
            })
            assert resp.status_code == 404, resp.text

            resp = await b.post("/api/maps", json={
                "name": "M2", "tag": "", "stages": [{
                    "name": "S1", "system_instructions": "x",
                    "verification_endpoint_id": a_ep,
                }],
            })
            assert resp.status_code == 404, resp.text

    @pytest.mark.asyncio
    async def test_scenario_rule_rejects_foreign_endpoint(self, admin_client):
        client_a, _, _ = admin_client
        a_ep = await self._victims_endpoint_id(client_a)

        async with self._second_client("bob_scen") as b:
            resp = await b.post("/api/scenario-rules", json={
                "name": "R", "prompt": "Summarize.", "endpoint_id": a_ep,
            })
            assert resp.status_code == 404, resp.text

            created = await b.post("/api/scenario-rules", json={
                "name": "R2", "prompt": "Summarize.",
            })
            assert created.status_code in (200, 201), created.text
            rule_id = created.json()["id"]
            resp = await b.put(f"/api/scenario-rules/{rule_id}", json={
                "endpoint_id": a_ep,
            })
            assert resp.status_code == 404, resp.text

    # -- controls: the sweep must not lock users out of their own endpoints -

    @pytest.mark.asyncio
    async def test_own_endpoint_accepted_everywhere(self, admin_client):
        async with self._second_client("bob_control") as b:
            own = await b.post("/api/endpoints", json={
                "name": "B endpoint", "base_url": "http://bob-endpoint:2222", "api_key": "sk-B",
            })
            assert own.status_code == 201, own.text
            own_id = own.json()["id"]

            s = await b.put("/api/summarization/settings", json={
                "summarization_endpoint_id": own_id,
            })
            assert s.status_code == 200, s.text

            k = await b.post("/api/api-keys", json={"label": "pin", "endpoint_id": own_id})
            assert k.status_code in (200, 201), k.text

            m = await b.post("/api/maps", json={
                "name": "M", "tag": "", "stages": [{
                    "name": "S1", "system_instructions": "x", "endpoint_id": own_id,
                }],
            })
            assert m.status_code in (200, 201), m.text

            r = await b.post("/api/scenario-rules", json={
                "name": "R", "prompt": "Summarize.", "endpoint_id": own_id,
            })
            assert r.status_code in (200, 201), r.text
