"""Tests for the update service, chained upgrades, and admin update endpoints."""

import json
import re
import subprocess
from pathlib import Path

import pytest

from app.services.updater import (
    _extract_changelog_section,
    _next_page_url,
    _parse_version,
    _release_zip_url,
    _version_from_changelog,
    build_chain,
    get_current_version,
)

ROOT = Path(__file__).resolve().parent.parent
RELEASES_URL = "https://api.github.com/repos/Tydorius/GitInTheVan/releases?per_page=100"
CHANGELOG_URL = "https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/CHANGELOG.md"


def _release(version: str, **extra):
    payload = {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/Tydorius/GitInTheVan/releases/tag/v{version}",
        "body": f"Notes for {version}",
        "zipball_url": f"https://example.com/{version}-source.zip",
        "assets": [
            {"name": f"GitInTheVan-{version}.zip",
             "browser_download_url": f"https://example.com/{version}.zip"}
        ],
    }
    payload.update(extra)
    return payload


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

    def test_patch_number_is_not_compared_as_text(self):
        """0.15.41 sorts above 0.15.4, which string comparison would get wrong."""
        assert _parse_version("0.15.4") < _parse_version("0.15.41")
        assert _parse_version("0.15.41") < _parse_version("0.15.42")

    def test_prerelease_suffix_is_not_supported(self):
        """Documents current behavior so a future change is deliberate."""
        assert _parse_version("1.2.3-rc1") == (0,)


class TestVersionFromChangelog:
    def test_first_header_wins(self):
        text = "# Changelog\n\n## [0.18.0] - 2026-07-15\n\n## [0.17.1] - 2026-07-15\n"
        assert _version_from_changelog(text) == "0.18.0"

    def test_multi_digit_major(self):
        assert _version_from_changelog("## [10.2.0] - 2030-01-01\n") == "10.2.0"

    def test_unreleased_header_is_skipped(self):
        text = "## [Unreleased]\n\n## [0.18.0] - 2026-07-15\n"
        assert _version_from_changelog(text) == "0.18.0"

    def test_crlf_input(self):
        assert _version_from_changelog("# Changelog\r\n\r\n## [0.18.0] - x\r\n") == "0.18.0"

    def test_no_headers_returns_none(self):
        assert _version_from_changelog("# Changelog\n\nNothing here.\n") is None

    def test_reads_the_real_changelog(self):
        """get_current_version must agree with the shipped CHANGELOG.md."""
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert get_current_version() == _version_from_changelog(text)


class TestGetCurrentVersion:
    def test_returns_string(self):
        v = get_current_version()
        assert isinstance(v, str)
        assert len(v) > 0

    def test_falls_back_to_metadata_when_changelog_missing(self, monkeypatch, tmp_path):
        """The Dockerfile does not copy CHANGELOG.md, so this path is load-bearing."""
        import app.services.updater as updater

        monkeypatch.setattr(updater, "_CHANGELOG_PATH", tmp_path / "absent.md")
        updater._clear_version_cache()
        assert updater.get_current_version() != ""

    def test_result_is_cached(self, monkeypatch, tmp_path):
        import app.services.updater as updater

        changelog = tmp_path / "CHANGELOG.md"
        changelog.write_text("## [1.2.3] - x\n", encoding="utf-8")
        monkeypatch.setattr(updater, "_CHANGELOG_PATH", changelog)
        updater._clear_version_cache()

        assert updater.get_current_version() == "1.2.3"
        changelog.write_text("## [9.9.9] - x\n", encoding="utf-8")
        assert updater.get_current_version() == "1.2.3"


class TestReleaseHelpers:
    def test_zip_asset_preferred_over_zipball(self):
        assert _release_zip_url(_release("0.18.0")) == "https://example.com/0.18.0.zip"

    def test_falls_back_to_zipball(self):
        assert _release_zip_url(_release("0.18.0", assets=[])) == (
            "https://example.com/0.18.0-source.zip"
        )

    def test_non_zip_assets_are_ignored(self):
        release = _release("0.18.0", assets=[
            {"name": "checksums.txt", "browser_download_url": "https://example.com/x.txt"}
        ])
        assert _release_zip_url(release) == "https://example.com/0.18.0-source.zip"

    def test_next_page_url_parsing(self):
        header = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
        assert _next_page_url(header) == "https://api.github.com/x?page=2"

    def test_next_page_url_absent_on_last_page(self):
        header = '<https://api.github.com/x?page=1>; rel="prev"'
        assert _next_page_url(header) == ""

    def test_next_page_url_empty_header(self):
        assert _next_page_url("") == ""


class TestBuildChain:
    # The actual published release set.
    RELEASES = [_release(v) for v in
                ["0.18.0", "0.16.1", "0.15.42", "0.15.41", "0.15.4", "0.15.3", "0.15.2", "0.15.1"]]

    def test_reported_case_0_15_42_to_0_18_0(self):
        """The upgrade that failed: two hops, not one jump."""
        chain = build_chain("0.15.42", self.RELEASES)
        assert [s["version"] for s in chain] == ["0.16.1", "0.18.0"]

    def test_single_hop(self):
        assert [s["version"] for s in build_chain("0.16.1", self.RELEASES)] == ["0.18.0"]

    def test_already_current_returns_empty(self):
        assert build_chain("0.18.0", self.RELEASES) == []

    def test_ahead_of_latest_returns_empty(self):
        assert build_chain("0.19.0", self.RELEASES) == []

    def test_long_chain_is_ascending(self):
        chain = [s["version"] for s in build_chain("0.15.3", self.RELEASES)]
        assert chain == ["0.15.4", "0.15.41", "0.15.42", "0.16.1", "0.18.0"]

    def test_version_only_in_changelog_is_skipped(self):
        """0.17.1 has a CHANGELOG entry but no release, so it is not installable."""
        chain = [s["version"] for s in build_chain("0.16.1", self.RELEASES)]
        assert "0.17.1" not in chain

    def test_unsorted_input_is_sorted(self):
        shuffled = [_release("0.18.0"), _release("0.15.1"), _release("0.16.1")]
        assert [s["version"] for s in build_chain("0.15.1", shuffled)] == ["0.16.1", "0.18.0"]

    def test_duplicate_versions_are_deduplicated(self):
        chain = build_chain("0.16.1", [_release("0.18.0"), _release("0.18.0")])
        assert len(chain) == 1

    def test_drafts_and_prereleases_excluded(self):
        releases = self.RELEASES + [
            _release("0.20.0", prerelease=True),
            _release("0.21.0", draft=True),
        ]
        assert [s["version"] for s in build_chain("0.16.1", releases)] == ["0.18.0"]

    def test_unparseable_tag_is_skipped(self):
        releases = [{"tag_name": "nightly", "zipball_url": "z"}, _release("0.18.0")]
        assert [s["version"] for s in build_chain("0.16.1", releases)] == ["0.18.0"]

    def test_release_without_download_is_skipped(self):
        releases = [{"tag_name": "v0.17.0", "assets": [], "zipball_url": ""}, _release("0.18.0")]
        assert [s["version"] for s in build_chain("0.16.1", releases)] == ["0.18.0"]

    def test_steps_start_pending_with_pinned_url(self):
        step = build_chain("0.16.1", self.RELEASES)[0]
        assert step["status"] == "pending"
        assert step["attempts"] == 0
        assert step["zip_url"] == "https://example.com/0.18.0.zip"
        assert step["tag"] == "v0.18.0"


class TestChangelogExtraction:
    SAMPLE_CHANGELOG = """# Changelog

## [0.16.0] - 2026-08-01

### Added

- Feature C

## [0.15.0] - 2026-07-15

### Added

- Feature B

## [0.14.0] - 2026-07-01

### Added

- Feature A
"""

    def test_extracts_single_version(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.15.0", "0.16.0")
        assert "0.16.0" in result
        assert "Feature C" in result
        assert "Feature B" not in result

    def test_extracts_multiple_versions(self):
        result = _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.14.0", "0.16.0")
        assert "Feature C" in result
        assert "Feature B" in result
        assert "Feature A" not in result

    def test_no_update_returns_empty(self):
        assert _extract_changelog_section(self.SAMPLE_CHANGELOG, "0.16.0", "0.16.0") == ""

    def test_no_headers_returns_empty(self):
        assert _extract_changelog_section("no headers here", "0.1.0", "0.2.0") == ""

    def test_version_not_found_returns_empty(self):
        assert _extract_changelog_section(self.SAMPLE_CHANGELOG, "9.9.9", "9.9.10") == ""


class TestReleaseListing:
    async def test_single_page(self, httpx_mock):
        from app.services.updater import get_releases

        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        releases = await get_releases()
        assert [_r["tag_name"] for _r in releases] == ["v0.18.0"]

    async def test_follows_link_header_pagination(self, httpx_mock):
        from app.services.updater import get_releases

        page2 = "https://api.github.com/repos/Tydorius/GitInTheVan/releases?per_page=100&page=2"
        httpx_mock.add_response(
            url=RELEASES_URL,
            json=[_release("0.18.0")],
            headers={"link": f'<{page2}>; rel="next"'},
            status_code=200,
        )
        httpx_mock.add_response(url=page2, json=[_release("0.16.1")], status_code=200)

        releases = await get_releases()
        assert {_r["tag_name"] for _r in releases} == {"v0.18.0", "v0.16.1"}

    async def test_drafts_and_prereleases_filtered_at_fetch(self, httpx_mock):
        from app.services.updater import get_releases

        httpx_mock.add_response(
            url=RELEASES_URL,
            json=[_release("0.18.0"), _release("0.19.0", prerelease=True)],
            status_code=200,
        )
        assert [_r["tag_name"] for _r in await get_releases()] == ["v0.18.0"]

    async def test_cache_prevents_a_second_request(self, httpx_mock):
        from app.services.updater import get_releases

        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        await get_releases()
        await get_releases()
        assert len(httpx_mock.get_requests()) == 1

    async def test_force_bypasses_cache(self, httpx_mock):
        from app.services.updater import get_releases

        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        await get_releases()
        await get_releases(force=True)
        assert len(httpx_mock.get_requests()) == 2


class TestChainState:
    @pytest.fixture
    def chain_dir(self, monkeypatch, tmp_path):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(updater, "_CHAIN_PATH", tmp_path / "update-chain.json")
        monkeypatch.setattr(updater, "_CHAIN_ARCHIVE_PATH", tmp_path / "update-chain-last.json")
        monkeypatch.setattr(updater, "_CHAIN_LOG_PATH", tmp_path / "update-chain.log")
        return tmp_path

    def test_round_trip(self, chain_dir):
        from app.services.updater import CHAIN_SCHEMA_VERSION, read_chain, write_chain

        write_chain({
            "schema_version": CHAIN_SCHEMA_VERSION,
            "status": "active",
            "from_version": "0.15.42",
            "target_version": "0.18.0",
            "steps": [{"version": "0.16.1"}, {"version": "0.18.0"}],
        })
        chain = read_chain()
        assert chain["from_version"] == "0.15.42"
        assert [s["version"] for s in chain["steps"]] == ["0.16.1", "0.18.0"]
        assert chain["steps"][0]["status"] == "pending"
        assert chain["steps"][0]["attempts"] == 0

    def test_written_with_lf_newlines(self, chain_dir):
        """The file gets pasted into bug reports; write_text would emit CRLF on Windows."""
        from app.services.updater import CHAIN_SCHEMA_VERSION, write_chain

        write_chain({"schema_version": CHAIN_SCHEMA_VERSION, "steps": []})
        assert b"\r\n" not in (chain_dir / "update-chain.json").read_bytes()

    def test_missing_file_returns_none(self, chain_dir):
        from app.services.updater import read_chain

        assert read_chain() is None

    def test_corrupt_json_returns_none_without_raising(self, chain_dir):
        from app.services.updater import read_chain

        (chain_dir / "update-chain.json").write_text("{not json", encoding="utf-8")
        assert read_chain() is None

    def test_future_schema_is_refused_not_guessed(self, chain_dir):
        from app.services.updater import read_chain

        (chain_dir / "update-chain.json").write_text(
            json.dumps({"schema_version": 2, "steps": [{"version": "9.9.9"}]}), encoding="utf-8"
        )
        chain = read_chain()
        assert chain["status"] == "unrecognized"
        assert chain["steps"] == []

    def test_missing_keys_get_defaults(self, chain_dir):
        from app.services.updater import CHAIN_SCHEMA_VERSION, read_chain

        (chain_dir / "update-chain.json").write_text(
            json.dumps({"schema_version": CHAIN_SCHEMA_VERSION}), encoding="utf-8"
        )
        chain = read_chain()
        assert chain["steps"] == []
        assert chain["status"] == "active"

    def test_idle_chain_expires(self, chain_dir):
        from app.services.updater import _chain_expired

        assert _chain_expired({"last_activity_at": "2020-01-01T00:00:00+00:00"}) is True

    def test_recent_chain_does_not_expire(self, chain_dir):
        from app.services.updater import _chain_expired, _now

        assert _chain_expired({"last_activity_at": _now()}) is False

    def test_unparseable_timestamp_does_not_expire(self, chain_dir):
        from app.services.updater import _chain_expired

        assert _chain_expired({"last_activity_at": "not-a-date"}) is False

    def test_clear_archives_then_removes(self, chain_dir):
        from app.services.updater import CHAIN_SCHEMA_VERSION, clear_chain, read_chain, write_chain

        write_chain({"schema_version": CHAIN_SCHEMA_VERSION, "steps": [], "target_version": "0.18.0"})
        clear_chain(status="completed")

        assert read_chain() is None
        archived = json.loads((chain_dir / "update-chain-last.json").read_text(encoding="utf-8"))
        assert archived["status"] == "completed"
        assert archived["target_version"] == "0.18.0"


class TestRunStepAndExecute:
    @pytest.fixture
    def staged(self, monkeypatch, tmp_path):
        """Isolate data/ and scripts/, and capture Popen instead of spawning."""
        import app.services.updater as updater

        data_dir = tmp_path / "data"
        scripts_dir = tmp_path / "scripts"
        data_dir.mkdir()
        scripts_dir.mkdir()
        for name in ("update-linux.sh", "update-macos.sh", "update-windows.bat"):
            (scripts_dir / name).write_text("stub", encoding="utf-8")

        monkeypatch.setattr(updater, "_DATA_DIR", data_dir)
        monkeypatch.setattr(updater, "_SCRIPTS_DIR", scripts_dir)
        monkeypatch.setattr(updater, "_REPO_ROOT", tmp_path)
        monkeypatch.setattr(updater, "_CHAIN_PATH", data_dir / "update-chain.json")
        monkeypatch.setattr(updater, "_CHAIN_ARCHIVE_PATH", data_dir / "update-chain-last.json")
        monkeypatch.setattr(updater, "_CHAIN_LOG_PATH", data_dir / "update-chain.log")

        launches = []

        def _fake_popen(args, **kwargs):
            # Read the chain from disk at launch time to prove the attempt was
            # persisted *before* the process could be killed.
            chain_path = data_dir / "update-chain.json"
            on_disk = json.loads(chain_path.read_text(encoding="utf-8")) if chain_path.exists() else None
            launches.append({"args": args, "kwargs": kwargs, "chain_on_disk": on_disk})
            return object()

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        return {"data": data_dir, "scripts": scripts_dir, "launches": launches}

    async def test_execute_freezes_chain_and_launches_first_hop(
        self, staged, httpx_mock, monkeypatch
    ):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.15.42")
        httpx_mock.add_response(
            url=RELEASES_URL,
            json=[_release("0.18.0"), _release("0.16.1"), _release("0.15.42")],
            status_code=200,
        )
        httpx_mock.add_response(url="https://example.com/0.16.1.zip", content=b"ZIPDATA")

        result = await updater.execute_update()
        assert result["success"] is True

        chain = json.loads((staged["data"] / "update-chain.json").read_text(encoding="utf-8"))
        assert chain["from_version"] == "0.15.42"
        assert chain["target_version"] == "0.18.0"
        assert [s["version"] for s in chain["steps"]] == ["0.16.1", "0.18.0"]
        assert chain["steps"][0]["status"] == "launched"
        assert chain["steps"][1]["status"] == "pending"

        # Only hop 1's zip is fetched; hop 2 downloads after hop 1 boots.
        downloads = [r for r in httpx_mock.get_requests() if "example.com" in str(r.url)]
        assert [str(r.url) for r in downloads] == ["https://example.com/0.16.1.zip"]
        assert (staged["data"] / "gitinthevan.zip").read_bytes() == b"ZIPDATA"

    async def test_attempt_is_persisted_before_launch(self, staged, httpx_mock, monkeypatch):
        """A hop that kills the process must still burn an attempt."""
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.1")
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        httpx_mock.add_response(url="https://example.com/0.18.0.zip", content=b"Z")

        await updater.execute_update()

        on_disk = staged["launches"][0]["chain_on_disk"]
        assert on_disk["steps"][0]["attempts"] == 1
        assert on_disk["steps"][0]["status"] == "launched"

    async def test_posix_launch_arguments(self, staged, httpx_mock, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater.sys, "platform", "linux")
        monkeypatch.setattr(updater.settings, "port", 8443)
        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.1")
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        httpx_mock.add_response(url="https://example.com/0.18.0.zip", content=b"Z")

        await updater.execute_update()

        launch = staged["launches"][0]
        assert launch["args"][0] == "bash"
        assert launch["args"][1].endswith("auto-update.sh")
        assert launch["args"][2:] == ["--auto", "8443"]
        assert launch["kwargs"]["start_new_session"] is True

    async def test_windows_launch_arguments(self, staged, httpx_mock, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater.sys, "platform", "win32")
        monkeypatch.setattr(updater.settings, "port", 8000)
        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.1")
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        httpx_mock.add_response(url="https://example.com/0.18.0.zip", content=b"Z")

        await updater.execute_update()

        launch = staged["launches"][0]
        assert launch["args"][0:2] == ["cmd", "/c"]
        assert launch["args"][2].endswith("auto-update.bat")
        assert launch["args"][3:] == ["--auto", "8000"]
        assert launch["kwargs"]["creationflags"] == subprocess.CREATE_NEW_CONSOLE

    async def test_refuses_when_a_chain_is_already_active(self, staged, monkeypatch):
        import app.services.updater as updater

        updater.write_chain({
            "schema_version": updater.CHAIN_SCHEMA_VERSION,
            "status": "active",
            "steps": [{"version": "0.18.0", "status": "launched"}],
        })

        result = await updater.execute_update()
        assert result["success"] is False
        assert "already in progress" in result["error"]
        assert staged["launches"] == []

    async def test_no_newer_release_returns_failure(self, staged, httpx_mock, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.18.0")
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)

        result = await updater.execute_update()
        assert result["success"] is False
        assert result["error"] == "No update available"
        assert staged["launches"] == []

    async def test_download_failure_marks_chain_failed(self, staged, httpx_mock, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.1")
        httpx_mock.add_response(url=RELEASES_URL, json=[_release("0.18.0")], status_code=200)
        httpx_mock.add_response(url="https://example.com/0.18.0.zip", status_code=404)

        result = await updater.execute_update()
        assert result["success"] is False

        chain = updater.read_chain()
        assert chain["status"] == "failed"
        assert chain["steps"][0]["status"] == "failed"
        assert staged["launches"] == []


class TestReconcileOnStartup:
    @pytest.fixture
    def chain_dir(self, monkeypatch, tmp_path):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(updater, "_CHAIN_PATH", tmp_path / "update-chain.json")
        monkeypatch.setattr(updater, "_CHAIN_ARCHIVE_PATH", tmp_path / "update-chain-last.json")
        monkeypatch.setattr(updater, "_CHAIN_LOG_PATH", tmp_path / "update-chain.log")
        return tmp_path

    @staticmethod
    def _chain(steps, **extra):
        import app.services.updater as updater

        chain = {
            "schema_version": updater.CHAIN_SCHEMA_VERSION,
            "status": "active",
            "created_at": updater._now(),
            "last_activity_at": updater._now(),
            "from_version": "0.15.42",
            "target_version": steps[-1]["version"],
            "error": "",
            "steps": steps,
        }
        chain.update(extra)
        return chain

    def test_installed_hop_advances_to_next(self, chain_dir, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.1")
        updater.write_chain(self._chain([
            {"version": "0.16.1", "status": "launched", "attempts": 1},
            {"version": "0.18.0", "status": "pending", "attempts": 0},
        ]))

        chain = updater.reconcile_chain_on_startup()
        assert chain is not None
        assert chain["steps"][0]["status"] == "installed"
        assert updater._next_pending_index(chain) == 1

    def test_newer_than_expected_still_counts_as_installed(self, chain_dir, monkeypatch):
        """A post-tag doc commit can make CHANGELOG lead the tag; >= not ==."""
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.16.2")
        updater.write_chain(self._chain([
            {"version": "0.16.1", "status": "launched", "attempts": 1},
            {"version": "0.18.0", "status": "pending", "attempts": 0},
        ]))

        chain = updater.reconcile_chain_on_startup()
        assert chain["steps"][0]["status"] == "installed"

    def test_final_hop_completes_and_archives(self, chain_dir, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.18.0")
        updater.write_chain(self._chain([
            {"version": "0.18.0", "status": "launched", "attempts": 1},
        ]))

        assert updater.reconcile_chain_on_startup() is None
        assert updater.read_chain() is None
        archived = json.loads((chain_dir / "update-chain-last.json").read_text(encoding="utf-8"))
        assert archived["status"] == "completed"

    def test_version_did_not_advance_halts_chain(self, chain_dir, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.15.42")
        updater.write_chain(self._chain([
            {"version": "0.16.1", "status": "launched", "attempts": 1},
            {"version": "0.18.0", "status": "pending", "attempts": 0},
        ]))

        assert updater.reconcile_chain_on_startup() is None
        assert updater.read_chain()["status"] == "version_mismatch"

    def test_max_attempts_marks_failed(self, chain_dir, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.15.42")
        updater.write_chain(self._chain([
            {"version": "0.16.1", "status": "launched", "attempts": 2},
        ]))

        assert updater.reconcile_chain_on_startup() is None
        chain = updater.read_chain()
        assert chain["status"] == "failed"
        assert chain["steps"][0]["status"] == "failed"

    def test_expired_chain_is_retained_not_deleted(self, chain_dir, monkeypatch):
        """Expiry only disables automatic resume; the admin can still retry."""
        import app.services.updater as updater

        monkeypatch.setattr(updater, "get_current_version", lambda: "0.15.42")
        updater.write_chain(self._chain(
            [{"version": "0.16.1", "status": "pending", "attempts": 0}],
            last_activity_at="2020-01-01T00:00:00+00:00",
        ))
        # write_chain refreshes the stamp, so age the file directly.
        raw = json.loads((chain_dir / "update-chain.json").read_text(encoding="utf-8"))
        raw["last_activity_at"] = "2020-01-01T00:00:00+00:00"
        (chain_dir / "update-chain.json").write_text(json.dumps(raw), encoding="utf-8")

        assert updater.reconcile_chain_on_startup() is None
        assert updater.read_chain()["status"] == "expired"

    def test_no_chain_file_is_a_noop(self, chain_dir):
        import app.services.updater as updater

        assert updater.reconcile_chain_on_startup() is None

    def test_unrecognized_schema_is_ignored(self, chain_dir):
        import app.services.updater as updater

        (chain_dir / "update-chain.json").write_text(
            json.dumps({"schema_version": 99}), encoding="utf-8"
        )
        assert updater.reconcile_chain_on_startup() is None

    def test_corrupt_chain_does_not_raise(self, chain_dir):
        """A corrupt file must never stop the server from booting."""
        import app.services.updater as updater

        (chain_dir / "update-chain.json").write_text("{{{", encoding="utf-8")
        assert updater.reconcile_chain_on_startup() is None

    def test_disabled_by_setting(self, chain_dir, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater.settings, "auto_update_chain_enabled", False)
        updater.write_chain(self._chain([{"version": "0.18.0", "status": "pending"}]))
        assert updater.start_chain_resume() is None


class TestWaitForServerReady:
    @pytest.fixture(autouse=True)
    def no_sleep(self, monkeypatch):
        import app.services.updater as updater

        async def _instant(_seconds):
            return None

        monkeypatch.setattr(updater.asyncio, "sleep", _instant)

    async def test_maintenance_page_html_is_not_ready(self, httpx_mock, monkeypatch):
        """The maintenance server binds the same port and serves HTML for every path."""
        import app.services.updater as updater

        httpx_mock.add_response(
            url=updater._health_url(), text="<html>updating</html>", status_code=200,
            is_reusable=True,
        )
        assert await updater._wait_for_server_ready(timeout=0.2) is False

    async def test_two_consecutive_successes_required(self, httpx_mock, monkeypatch):
        import app.services.updater as updater

        httpx_mock.add_response(url=updater._health_url(), json={"status": "ok"}, is_reusable=True)
        assert await updater._wait_for_server_ready(timeout=30.0) is True

    async def test_non_200_is_not_ready(self, httpx_mock):
        import app.services.updater as updater

        httpx_mock.add_response(url=updater._health_url(), status_code=503, is_reusable=True)
        assert await updater._wait_for_server_ready(timeout=0.2) is False

    def test_health_url_uses_loopback_for_wildcard_host(self, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater.settings, "host", "0.0.0.0")
        monkeypatch.setattr(updater.settings, "port", 8000)
        monkeypatch.setattr(updater.settings, "ssl_certfile", "")
        monkeypatch.setattr(updater.settings, "ssl_keyfile", "")
        assert updater._health_url() == "http://127.0.0.1:8000/health"

    def test_health_url_honours_explicit_host_and_tls(self, monkeypatch):
        import app.services.updater as updater

        monkeypatch.setattr(updater.settings, "host", "10.0.0.5")
        monkeypatch.setattr(updater.settings, "port", 8443)
        monkeypatch.setattr(updater.settings, "ssl_certfile", "c.pem")
        monkeypatch.setattr(updater.settings, "ssl_keyfile", "k.pem")
        assert updater._health_url() == "https://10.0.0.5:8443/health"


class TestUpdateCheckAPI:
    async def test_update_check_requires_admin(self, client):
        resp = await client.get("/api/admin/update/check")
        assert resp.status_code in (401, 403)

    async def test_update_check_returns_response(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(url=RELEASES_URL, json=[_release("99.99.99")], status_code=200)
        httpx_mock.add_response(
            url=CHANGELOG_URL,
            text="## [99.99.99]\n\n### Added\n\n- Test feature\n",
            status_code=200,
        )

        resp = await client.get("/api/admin/update/check")
        assert resp.status_code == 200
        data = resp.json()
        assert data["latest_version"] == "99.99.99"
        assert data["update_available"] is True
        assert data["zip_url"] == "https://example.com/99.99.99.zip"
        assert data["step_count"] == 1

    async def test_update_check_reports_multi_step_upgrade(self, admin_client, httpx_mock):
        """The Admin UI needs the hop count before the user confirms."""
        client, _, _ = admin_client

        httpx_mock.add_response(
            url=RELEASES_URL,
            json=[_release("99.99.99"), _release("99.0.0")],
            status_code=200,
        )
        httpx_mock.add_response(url=CHANGELOG_URL, text="## [99.99.99]\n", status_code=200)

        data = (await client.get("/api/admin/update/check")).json()
        assert data["step_count"] == 2

    async def test_update_check_no_update_available(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        current = get_current_version()

        httpx_mock.add_response(url=RELEASES_URL, json=[_release(current)], status_code=200)

        data = (await client.get("/api/admin/update/check")).json()
        assert data["update_available"] is False
        assert data["latest_version"] == current
        assert data["step_count"] == 0

    async def test_update_check_github_error(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(url=RELEASES_URL, status_code=403)

        data = (await client.get("/api/admin/update/check")).json()
        assert data["update_available"] is False
        assert data["error"]

    async def test_download_info_requires_admin(self, client):
        resp = await client.get("/api/admin/update/download-info")
        assert resp.status_code in (401, 403)

    async def test_download_info_returns_data(self, admin_client, httpx_mock):
        client, _, _ = admin_client

        httpx_mock.add_response(url=RELEASES_URL, json=[_release("99.99.99")], status_code=200)
        httpx_mock.add_response(url=CHANGELOG_URL, text="## [99.99.99]\n", status_code=200)

        data = (await client.get("/api/admin/update/download-info")).json()
        assert data["zip_url"] == "https://example.com/99.99.99.zip"
        assert data["latest_version"] == "99.99.99"
        assert "instructions" in data

    async def test_execute_update_requires_admin(self, client):
        resp = await client.post("/api/admin/update/execute")
        assert resp.status_code in (401, 403)

    async def test_execute_update_no_release(self, admin_client, httpx_mock):
        client, _, _ = admin_client
        current = get_current_version()

        httpx_mock.add_response(url=RELEASES_URL, json=[_release(current)], status_code=200)

        data = (await client.post("/api/admin/update/execute")).json()
        assert data["success"] is False


class TestUpdateChainAPI:
    @pytest.fixture
    def chain_dir(self, monkeypatch, tmp_path):
        import app.services.updater as updater

        monkeypatch.setattr(updater, "_DATA_DIR", tmp_path)
        monkeypatch.setattr(updater, "_CHAIN_PATH", tmp_path / "update-chain.json")
        monkeypatch.setattr(updater, "_CHAIN_ARCHIVE_PATH", tmp_path / "update-chain-last.json")
        monkeypatch.setattr(updater, "_CHAIN_LOG_PATH", tmp_path / "update-chain.log")
        return tmp_path

    async def test_requires_admin(self, client, chain_dir):
        assert (await client.get("/api/admin/update/chain")).status_code in (401, 403)
        assert (await client.delete("/api/admin/update/chain")).status_code in (401, 403)
        assert (await client.post("/api/admin/update/chain/resume")).status_code in (401, 403)

    async def test_reports_inactive_with_no_chain(self, admin_client, chain_dir):
        client, _, _ = admin_client
        data = (await client.get("/api/admin/update/chain")).json()
        assert data["active"] is False
        assert data["steps"] == []

    async def test_reports_progress(self, admin_client, chain_dir):
        import app.services.updater as updater

        client, _, _ = admin_client
        updater.write_chain({
            "schema_version": updater.CHAIN_SCHEMA_VERSION,
            "status": "active",
            "from_version": "0.15.42",
            "target_version": "0.18.0",
            "steps": [
                {"version": "0.16.1", "status": "installed", "attempts": 1},
                {"version": "0.18.0", "status": "pending", "attempts": 0},
            ],
        })

        data = (await client.get("/api/admin/update/chain")).json()
        assert data["active"] is True
        assert data["current_step"] == 2
        assert data["total_steps"] == 2
        assert data["from_version"] == "0.15.42"
        assert [s["status"] for s in data["steps"]] == ["installed", "pending"]

    async def test_abort_clears_and_archives(self, admin_client, chain_dir):
        import app.services.updater as updater

        client, _, _ = admin_client
        updater.write_chain({
            "schema_version": updater.CHAIN_SCHEMA_VERSION,
            "status": "failed",
            "steps": [{"version": "0.18.0", "status": "failed"}],
        })

        result = (await client.delete("/api/admin/update/chain")).json()
        assert result["success"] is True
        assert updater.read_chain() is None
        assert (chain_dir / "update-chain-last.json").exists()

    async def test_abort_with_no_chain_reports_failure(self, admin_client, chain_dir):
        client, _, _ = admin_client
        result = (await client.delete("/api/admin/update/chain")).json()
        assert result["success"] is False

    async def test_resume_reuses_the_frozen_plan(self, admin_client, chain_dir, monkeypatch):
        """A retry must not re-resolve releases -- it lands on the approved version."""
        import app.services.updater as updater

        client, _, _ = admin_client
        updater.write_chain({
            "schema_version": updater.CHAIN_SCHEMA_VERSION,
            "status": "failed",
            "error": "boom",
            "target_version": "0.18.0",
            "steps": [{
                "version": "0.18.0", "status": "failed", "attempts": 2,
                "error": "boom", "zip_url": "https://example.com/0.18.0.zip",
            }],
        })

        captured = {}

        async def _fake_run_step(chain, index):
            captured["chain"] = chain
            captured["index"] = index
            return {"success": True, "message": "ok"}

        monkeypatch.setattr(updater, "_run_step", _fake_run_step)

        result = (await client.post("/api/admin/update/chain/resume")).json()
        assert result["success"] is True
        assert captured["index"] == 0
        assert captured["chain"]["steps"][0]["attempts"] == 0
        assert captured["chain"]["steps"][0]["zip_url"] == "https://example.com/0.18.0.zip"

    async def test_resume_with_no_chain_reports_failure(self, admin_client, chain_dir):
        client, _, _ = admin_client
        result = (await client.post("/api/admin/update/chain/resume")).json()
        assert result["success"] is False


class TestUpdateScriptGuardRails:
    """Plain-text assertions over the shell scripts.

    These cannot be exercised in CI, and they are the only code in the repo that
    can destroy a user's install, so lock in the invariants chaining depends on.
    """

    SCRIPTS = ["update-linux.sh", "update-macos.sh", "update-windows.bat"]

    @staticmethod
    def _text(name: str) -> str:
        return (ROOT / "scripts" / name).read_text(encoding="utf-8", errors="ignore")

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_never_deletes_the_chain_file(self, name):
        """The chain lives in data/ precisely so it survives every hop."""
        text = self._text(name)
        for line in text.splitlines():
            if re.search(r"\b(rm|del|rmdir|Remove-Item)\b", line):
                assert "update-chain" not in line, f"{name} deletes chain state: {line.strip()}"

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_rotates_the_updater_log(self, name):
        """Without rotation each hop destroys the previous hop's evidence."""
        assert "update-logs" in self._text(name), f"{name} does not rotate updater.log"

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_accepts_auto_and_port_arguments(self, name):
        text = self._text(name)
        assert "--auto" in text, f"{name} does not handle --auto"
        assert "GITV_PORT" in text or "%~2" in text or '${2:-' in text, (
            f"{name} does not accept a port argument"
        )

    def test_windows_has_no_unguarded_pause(self):
        """`pause` under CREATE_NEW_CONSOLE with detached stdin blocks forever."""
        for line in self._text("update-windows.bat").splitlines():
            stripped = line.strip()
            if stripped.lower() == "pause":
                pytest.fail(f"unguarded pause in update-windows.bat: {line!r}")

    def test_line_endings_match_gitattributes(self):
        for name in self.SCRIPTS:
            raw = (ROOT / "scripts" / name).read_bytes()
            if name.endswith(".bat"):
                assert b"\r\n" in raw, f"{name} must use CRLF"
            else:
                assert b"\r" not in raw, f"{name} must use LF"

    def test_macos_and_linux_scripts_stay_in_sync(self):
        """They are byte-identical apart from a few platform-specific lines."""
        import difflib

        linux = self._text("update-linux.sh").splitlines()
        macos = self._text("update-macos.sh").splitlines()
        differing = [
            line for line in difflib.unified_diff(linux, macos, lineterm="", n=0)
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        ]
        assert len(differing) <= 8, (
            "update-macos.sh has drifted from update-linux.sh; mirror changes into both:\n"
            + "\n".join(differing)
        )

    # The maintenance page binds the server's port for the whole update, and the
    # teardown below is the only code in the product that ever releases it. If
    # that teardown fails the install can never serve again -- not on this run
    # and not on any later one, because every later run stages its script from
    # the install that is already broken. 0.18.0 freed the port with a bare
    # `netstat`; on a machine whose PATH had lost System32 that lookup failed and
    # stranded the install permanently. The next three tests keep the two
    # properties that turned that into a brick from coming back.

    def test_windows_never_calls_netstat_bare(self):
        """netstat must be absolute-path: PATH is inherited and may be broken."""
        for lineno, line in enumerate(self._text("update-windows.bat").splitlines(), 1):
            code = line.strip()
            if not code or code.upper().startswith("REM"):
                continue
            for match in re.finditer(r"(?i)netstat", code):
                before = code[: match.start()].lower()
                assert before.endswith("system32\\"), (
                    f"update-windows.bat:{lineno} invokes netstat without an absolute "
                    f"path, so a broken PATH can strand the port: {code!r}"
                )

    @pytest.mark.parametrize("name", ["update-linux.sh", "update-macos.sh"])
    def test_unix_port_scanning_is_isolated_and_guarded(self, name):
        """Every port scan lives in kill_port_holders, which tolerates a missing tool."""
        lines = self._text(name).splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith("kill_port_holders() {"))
        end = next(i for i, l in enumerate(lines) if i > start and l == "}")
        body = "\n".join(lines[start : end + 1])
        assert "command -v lsof" in body, (
            f"{name}: kill_port_holders calls lsof without checking it exists; lsof is "
            "not installed on many minimal distros"
        )

        stray = [
            (i + 1, l.strip())
            for i, l in enumerate(lines)
            if not (start <= i <= end)
            and not l.strip().startswith("#")
            and re.search(r"(?<![\w-])(lsof|fuser|ss)\b", l)
        ]
        assert not stray, (
            f"{name} scans for port holders outside the guarded helper, so a missing "
            f"tool becomes fatal again: {stray}"
        )

    @pytest.mark.parametrize("name", SCRIPTS)
    def test_maintenance_page_teardown_is_pid_based(self, name):
        """Scanning is the fallback; the recorded PID is the primary path."""
        text = self._text(name)
        assert "_maintenance.pid" in text, (
            f"{name} does not record the maintenance page PID, leaving a port scan as "
            "the only way to free the port"
        )
        remover = "del " if name.endswith(".bat") else "rm -f "
        assert any(
            remover in line and "_maintenance.pid" in line for line in text.splitlines()
        ), (
            f"{name} never removes _maintenance.pid; a stale file would misdirect the "
            "next teardown into killing an unrelated recycled PID"
        )

    @pytest.mark.parametrize("name", ["update-windows.bat", "deploy-windows.bat"])
    def test_windows_scripts_prepend_system32_to_path(self, name):
        """Prepended, and before the first tool that needs it."""
        lines = self._text(name).splitlines()
        hardened = next(
            (
                i
                for i, l in enumerate(lines)
                if l.strip().lower().startswith('set "path=') and "system32" in l.lower()
            ),
            None,
        )
        assert hardened is not None, (
            f"{name} does not put System32 on PATH; updater.py launches it via subprocess "
            "so it inherits whatever PATH the server had"
        )
        value = lines[hardened].split("=", 1)[1].lower()
        assert value.startswith("%systemroot%\\system32"), (
            f"{name} appends System32 rather than prepending it, so a stray netstat.exe "
            f"earlier in PATH would win: {lines[hardened]!r}"
        )

        needs_path = re.compile(
            r"(?i)(?<![\\/\w])(findstr|where|ping|timeout|taskkill|powershell|tar)\b"
        )
        first_use = next(
            (
                i
                for i, l in enumerate(lines)
                if not l.strip().upper().startswith("REM") and needs_path.search(l)
            ),
            None,
        )
        if first_use is not None:
            assert hardened < first_use, (
                f"{name}:{first_use + 1} uses a System32 tool before PATH is hardened at "
                f"line {hardened + 1}: {lines[first_use]!r}"
            )
