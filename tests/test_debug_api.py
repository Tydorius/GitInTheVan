"""API tests for saved runs, retention, export and comparison (Phase 22 Stage B)."""

from __future__ import annotations

import json

import pytest

from app.services.debug import MAX_DEBUG_EXCHANGES, capture_exchange, list_exchanges


def pipeline(cantrips=None, stages=None, totals=None) -> dict:
    return {
        "schema_version": 2,
        "tags": [],
        "original_messages": json.dumps([{"role": "user", "content": "hi"}]),
        "stages": stages or [],
        "run": {
            "started_at": "", "source": "live", "replay_of": "",
            "cantrips": cantrips or [], "llm_calls": [],
            "totals": totals or {},
        },
    }


async def seed(user_id: str, count: int = 1, **kwargs) -> None:
    for i in range(count):
        await capture_exchange(
            user_id=user_id, chat_id=f"chat-{i}", model="gpt-4o",
            pipeline_data=pipeline(**kwargs),
            response_content=f"response {i}",
            verification_data={"approved": True, "retries_used": 0, "check_history": []},
        )


async def user_id_of(client, token) -> str:
    resp = await client.get("/api/auth/me")
    return resp.json()["id"]


@pytest.mark.asyncio
class TestSavedRuns:
    async def test_saving_pins_a_run_against_the_retention_prune(self, admin_client):
        """A comparison baseline must not be evicted by later traffic."""
        client, token, _ = admin_client
        uid = await user_id_of(client, token)

        await seed(uid, 1)
        target = (await client.get("/api/debug")).json()["exchanges"][0]["id"]

        resp = await client.post(f"/api/debug/{target}/save", json={"label": "Baseline"})
        assert resp.status_code == 200
        assert resp.json()["saved"] is True
        assert resp.json()["label"] == "Baseline"

        # Push well past the retention limit.
        await seed(uid, MAX_DEBUG_EXCHANGES + 5)

        remaining = {e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]}
        assert target in remaining

    async def test_saved_runs_survive_clear_all(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 3)

        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]
        await client.post(f"/api/debug/{ids[0]}/save", json={"label": "keep"})

        assert (await client.delete("/api/debug")).status_code == 204
        after = (await client.get("/api/debug")).json()["exchanges"]
        assert [e["id"] for e in after] == [ids[0]]

    async def test_clear_all_can_be_told_to_include_saved_runs(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 2)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]
        await client.post(f"/api/debug/{ids[0]}/save", json={})

        await client.delete("/api/debug?include_saved=true")
        assert (await client.get("/api/debug")).json()["exchanges"] == []

    async def test_the_save_cap_refuses_rather_than_evicting(self, admin_client):
        """The user chose which runs matter; dropping the oldest saved one to
        make room would throw away a deliberate choice."""
        client, token, _ = admin_client
        uid = await user_id_of(client, token)

        await client.put("/api/admin/settings", json={"max_saved_debug_runs": 2})
        await seed(uid, 3)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]

        assert (await client.post(f"/api/debug/{ids[0]}/save", json={})).status_code == 200
        assert (await client.post(f"/api/debug/{ids[1]}/save", json={})).status_code == 200

        refused = await client.post(f"/api/debug/{ids[2]}/save", json={})
        assert refused.status_code == 409
        assert "limit is 2" in refused.json()["detail"]

        # The two already saved are untouched.
        listing = (await client.get("/api/debug")).json()
        assert listing["saved_count"] == 2
        assert listing["max_saved"] == 2

    async def test_unsaving_frees_a_slot(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await client.put("/api/admin/settings", json={"max_saved_debug_runs": 1})
        await seed(uid, 2)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]

        await client.post(f"/api/debug/{ids[0]}/save", json={})
        assert (await client.post(f"/api/debug/{ids[1]}/save", json={})).status_code == 409

        assert (await client.delete(f"/api/debug/{ids[0]}/save")).status_code == 200
        assert (await client.post(f"/api/debug/{ids[1]}/save", json={})).status_code == 200

    async def test_saved_only_filter(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 3)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]
        await client.post(f"/api/debug/{ids[1]}/save", json={})

        only = (await client.get("/api/debug?saved_only=true")).json()["exchanges"]
        assert [e["id"] for e in only] == [ids[1]]


@pytest.mark.asyncio
class TestRunMaintenance:
    async def test_rename(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1)
        rid = (await client.get("/api/debug")).json()["exchanges"][0]["id"]

        resp = await client.patch(f"/api/debug/{rid}", json={"label": "Variant B"})
        assert resp.status_code == 200
        assert resp.json()["label"] == "Variant B"

    async def test_delete_one(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 2)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]

        assert (await client.delete(f"/api/debug/{ids[0]}")).status_code == 204
        remaining = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]
        assert ids[0] not in remaining and ids[1] in remaining

    async def test_deleting_an_unknown_run_is_a_404(self, admin_client):
        client, _, _ = admin_client
        assert (await client.delete("/api/debug/does-not-exist")).status_code == 404

    async def test_runs_are_auto_labelled_from_what_they_activated(self, admin_client):
        """Otherwise the picker is a column of identical timestamps."""
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1, cantrips=[
            {"id": "c1", "name": "Dice Controller", "position": "pre_driver", "triggered": True},
        ])
        item = (await client.get("/api/debug")).json()["exchanges"][0]
        assert item["label"] == "Dice Controller"

    async def test_has_verification_is_true_when_verification_ran(self, admin_client):
        """This flag was bool(dict) and the map path passed {}, so it read false
        on every map run."""
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1)
        assert (await client.get("/api/debug")).json()["exchanges"][0]["has_verification"] is True


@pytest.mark.asyncio
class TestExport:
    async def test_json_export_downloads_with_a_filename(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1)
        rid = (await client.get("/api/debug")).json()["exchanges"][0]["id"]

        resp = await client.get(f"/api/debug/{rid}/export?format=json")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["content-disposition"]
        assert json.loads(resp.text)["kind"] == "debug_run"

    async def test_markdown_export(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1)
        rid = (await client.get("/api/debug")).json()["exchanges"][0]["id"]

        resp = await client.get(f"/api/debug/{rid}/export?format=markdown")
        assert resp.status_code == 200
        assert resp.text.startswith("#")

    async def test_an_unknown_format_is_rejected(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 1)
        rid = (await client.get("/api/debug")).json()["exchanges"][0]["id"]
        assert (await client.get(f"/api/debug/{rid}/export?format=pdf")).status_code == 400


@pytest.mark.asyncio
class TestCompare:
    async def _two_runs(self, client, uid):
        await seed(uid, 2)
        return [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]

    async def test_comparing_two_runs(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)

        resp = await client.post("/api/debug/compare", json={"ids": ids, "baseline_id": ids[1]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["baseline_id"] == ids[1]
        assert body["order"][0] == ids[1]
        assert set(body["diffs"]) == {ids[0]}

    async def test_baseline_defaults_to_the_first_id(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)
        body = (await client.post("/api/debug/compare", json={"ids": ids})).json()
        assert body["baseline_id"] == ids[0]

    async def test_fewer_than_two_runs_is_rejected(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)
        assert (await client.post("/api/debug/compare", json={"ids": ids[:1]})).status_code == 400

    async def test_more_than_four_runs_is_rejected(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, 5)
        ids = [e["id"] for e in (await client.get("/api/debug")).json()["exchanges"]]
        resp = await client.post("/api/debug/compare", json={"ids": ids[:5]})
        assert resp.status_code == 400
        assert "At most 4" in resp.json()["detail"]

    async def test_the_same_run_twice_is_rejected(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)
        resp = await client.post("/api/debug/compare", json={"ids": [ids[0], ids[0]]})
        assert resp.status_code == 400

    async def test_a_baseline_outside_the_selection_is_rejected(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)
        resp = await client.post(
            "/api/debug/compare", json={"ids": ids, "baseline_id": "other"}
        )
        assert resp.status_code == 400

    async def test_comparison_export(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        ids = await self._two_runs(client, uid)

        resp = await client.post("/api/debug/compare/export?format=markdown", json={"ids": ids})
        assert resp.status_code == 200
        assert "Debug Run Comparison" in resp.text


@pytest.mark.asyncio
class TestIsolationBetweenUsers:
    async def test_a_user_cannot_read_another_users_run(self, admin_client):
        """Every debug query filters on user_id; this is the guard on that."""
        client, _, _ = admin_client
        uid = await user_id_of(client, None)
        await seed(uid, 1)
        rid = (await client.get("/api/debug")).json()["exchanges"][0]["id"]

        await client.post("/api/users", json={"username": "second", "password": "password2"})
        login = await client.post(
            "/api/auth/login", json={"username": "second", "password": "password2"}
        )
        assert login.status_code == 200
        other = {"Authorization": f"Bearer {login.json()['access_token']}"}

        assert (await client.get(f"/api/debug/{rid}", headers=other)).status_code == 404
        assert (await client.get("/api/debug", headers=other)).json()["exchanges"] == []
        assert (await client.delete(f"/api/debug/{rid}", headers=other)).status_code == 404
        assert (await client.post(f"/api/debug/{rid}/save", json={}, headers=other)).status_code == 404


@pytest.mark.asyncio
class TestRetentionAccounting:
    async def test_unsaved_runs_are_pruned_to_the_limit(self, admin_client):
        client, token, _ = admin_client
        uid = await user_id_of(client, token)
        await seed(uid, MAX_DEBUG_EXCHANGES + 6)
        runs = await list_exchanges(uid, limit=100)
        assert len(runs) == MAX_DEBUG_EXCHANGES
