"""Tests for update check service and admin update endpoints."""

import pytest

from app.services.updater import _parse_version, get_current_version


class TestVersionParsing:
    def test_parse_simple(self):
        assert _parse_version("0.14.5") == (0, 14, 5)

    def test_parse_with_v_prefix(self):
        assert _parse_version("v1.2.3") == (1, 2, 3)

    def test_parse_two_parts(self):
        assert _parse_version("1.0") == (1, 0)

    def test_parse_invalid_returns_zero(self):
        assert _parse_version("invalid") == (0,)

    def test_parse_empty(self):
        assert _parse_version("") == (0,)

    def test_comparison(self):
        assert _parse_version("0.14.5") < _parse_version("0.15.0")
        assert _parse_version("1.0.0") > _parse_version("0.99.99")
        assert _parse_version("0.14.5") == _parse_version("0.14.5")


class TestGetCurrentVersion:
    def test_returns_string(self):
        v = get_current_version()
        assert isinstance(v, str)
        assert len(v) > 0


class TestUpdateCheckAPI:
    @pytest.mark.asyncio
    async def test_update_check_requires_admin(self, client):
        resp = await client.get("/api/admin/update/check")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_update_check_returns_response(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(
            url="https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest",
            json={
                "tag_name": "v99.99.99",
                "html_url": "https://github.com/Tydorius/GitInTheVan/releases/tag/v99.99.99",
                "body": "Test release notes",
                "assets": [
                    {"name": "gitinthevan.zip", "browser_download_url": "https://example.com/zip"}
                ],
            },
            status_code=200,
        )

        resp = await client.get("/api/admin/update/check")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_version" in data
        assert "latest_version" in data
        assert data["latest_version"] == "99.99.99"
        assert data["update_available"] is True
        assert data["release_url"] == "https://github.com/Tydorius/GitInTheVan/releases/tag/v99.99.99"
        assert data["zip_url"] == "https://example.com/zip"

    @pytest.mark.asyncio
    async def test_update_check_no_update_available(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        current = get_current_version()

        httpx_mock.add_response(
            url="https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest",
            json={
                "tag_name": f"v{current}",
                "html_url": "https://github.com/Tydorius/GitInTheVan",
                "body": "Same version",
                "assets": [],
            },
            status_code=200,
        )

        resp = await client.get("/api/admin/update/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert data["latest_version"] == current

    @pytest.mark.asyncio
    async def test_update_check_github_error(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(
            url="https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest",
            status_code=403,
        )

        resp = await client.get("/api/admin/update/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["update_available"] is False
        assert "error" in data

    @pytest.mark.asyncio
    async def test_download_info_requires_admin(self, client):
        resp = await client.get("/api/admin/update/download-info")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_download_info_returns_data(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(
            url="https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest",
            json={
                "tag_name": "v99.99.99",
                "html_url": "https://github.com/Tydorius/GitInTheVan",
                "body": "Release",
                "assets": [{"name": "release.zip", "browser_download_url": "https://example.com/dl"}],
            },
            status_code=200,
        )

        resp = await client.get("/api/admin/update/download-info")
        assert resp.status_code == 200
        data = resp.json()
        assert "zip_url" in data
        assert "current_version" in data
        assert "latest_version" in data
        assert "instructions" in data
        assert data["zip_url"] == "https://example.com/dl"
