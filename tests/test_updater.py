"""Tests for update check service and admin update endpoints."""

import pytest

from app.services.updater import _extract_changelog_section, _parse_version, get_current_version


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


class TestChangelogExtraction:
    SAMPLE_CHANGELOG = """# Changelog

## [0.16.0] - 2026-08-01

### Added

- Feature A
- Feature B

### Fixed

- Bug X

## [0.15.0] - 2026-07-07

### Added

- Feature C

## [0.14.5] - 2026-06-20

### Added

- Feature D

## [0.14.0] - 2026-06-01
"""

    def test_single_version_jump(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.15.0", "0.16.0")
        assert "Feature A" in result
        assert "Feature B" in result
        assert "Bug X" in result
        assert "Feature C" not in result
        assert "## [0.16.0]" in result

    def test_multi_version_jump(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.14.0", "0.16.0")
        assert "Feature A" in result
        assert "Feature C" in result
        assert "Feature D" in result
        assert "## [0.16.0]" in result
        assert "## [0.15.0]" in result
        assert "## [0.14.5]" in result

    def test_no_update_returns_empty(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.16.0", "0.16.0")
        assert result == ""

    def test_no_headers_returns_empty(self):
        result = _extract_changelog_section("No headers here", "0.1.0", "0.2.0")
        assert result == ""

    def test_version_not_found_returns_empty(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "9.9.9", "9.9.10")
        assert result == ""


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
        httpx_mock.add_response(
            url="https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/CHANGELOG.md",
            text="## [99.99.99]\n\n### Added\n\n- Test feature\n",
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

        httpx_mock.add_response(
            url="https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/CHANGELOG.md",
            text="## [99.99.99]\n\n### Added\n\n- Test\n",
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
