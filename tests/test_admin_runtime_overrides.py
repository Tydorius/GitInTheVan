"""Admin-owned runtime overrides (0.33.1).

The Admin UI's rate-limit fields wrote to ``admin_settings`` while the rate
limiter read ``app.config.settings`` on every request -- the fields were a
silent no-op from the day they shipped. These pin the fix from both sides:
a PUT applies immediately, and startup re-applies persisted values so a
restart cannot silently revert an admin's change.
"""
import pytest

from app.config import settings as runtime_settings


@pytest.mark.asyncio
class TestAdminRuntimeOverrides:
    async def test_rate_limit_change_applies_immediately(self, admin_client):
        client, _, _ = admin_client
        original_proxy = runtime_settings.rate_limit_proxy_per_min
        original_api = runtime_settings.rate_limit_api_per_min
        try:
            resp = await client.put("/api/admin/settings", json={
                "rate_limit_proxy_per_min": 7,
                "rate_limit_api_per_min": 8,
            })
            assert resp.status_code == 200, resp.text
            # The limiter reads these on every request; if the PUT does not
            # reach them, the admin field changes nothing.
            assert runtime_settings.rate_limit_proxy_per_min == 7
            assert runtime_settings.rate_limit_api_per_min == 8
        finally:
            runtime_settings.rate_limit_proxy_per_min = original_proxy
            runtime_settings.rate_limit_api_per_min = original_api

    async def test_startup_reapplies_persisted_overrides(self, admin_client):
        client, _, _ = admin_client
        from app.services.admin import apply_admin_overrides_at_startup

        original_proxy = runtime_settings.rate_limit_proxy_per_min
        try:
            resp = await client.put("/api/admin/settings", json={
                "rate_limit_proxy_per_min": 11,
            })
            assert resp.status_code == 200, resp.text

            # A restart reloads env seeds into runtime_settings; the DB row
            # is what must win afterwards.
            runtime_settings.rate_limit_proxy_per_min = 60
            await apply_admin_overrides_at_startup()
            assert runtime_settings.rate_limit_proxy_per_min == 11
        finally:
            runtime_settings.rate_limit_proxy_per_min = original_proxy
            # Leave the stored row back at the seed so other tests are not
            # limited by this one.
            await client.put("/api/admin/settings", json={
                "rate_limit_proxy_per_min": original_proxy,
            })
