"""Check for GitInTheVan updates from GitHub releases."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest"
CHANGELOG_URL = "https://raw.githubusercontent.com/Tydorius/GitInTheVan/main/CHANGELOG.md"

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"


def get_current_version() -> str:
    """Get the current installed version from package metadata."""
    try:
        import importlib.metadata
        return importlib.metadata.version("gitinthevan")
    except Exception:
        return "0.0.0"


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a version string like '0.14.5' into a comparable tuple."""
    parts = v.lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def _extract_changelog_section(changelog: str, current: str, latest: str) -> str:
    """Extract changelog entries between current and latest version headers.

    Finds all version headers (## [X.Y.Z] ...), collects every section
    whose version is greater than current and less than or equal to latest.
    Returns the combined markdown text (max 4000 chars).
    """
    header_re = re.compile(r"^##\s*\[([\d.]+)\]", re.MULTILINE)
    matches = list(header_re.finditer(changelog))
    if not matches:
        return ""

    cur_ver = _parse_version(current)
    lat_ver = _parse_version(latest)

    sections: list[str] = []
    for i, m in enumerate(matches):
        ver = _parse_version(m.group(1))
        if ver <= cur_ver:
            continue
        if ver > lat_ver:
            break

        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(changelog)
        block = changelog[start:end].strip()
        if block:
            sections.append(block)

    result = "\n\n".join(sections)
    return result[:4000]


async def _fetch_changelog(client: httpx.AsyncClient) -> str:
    """Fetch CHANGELOG.md from the repo."""
    resp = await client.get(CHANGELOG_URL)
    if resp.status_code == 200:
        return resp.text
    return ""


async def check_for_update() -> dict[str, Any]:
    """Check GitHub for the latest release.

    Returns dict with:
        current_version: str
        latest_version: str
        update_available: bool
        release_url: str
        release_notes: str
        zip_url: str
    """
    current = get_current_version()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                GITHUB_API,
                headers={"Accept": "application/vnd.github+json"},
            )
            if resp.status_code != 200:
                return {
                    "current_version": current,
                    "latest_version": current,
                    "update_available": False,
                    "error": f"GitHub API returned {resp.status_code}",
                }

            data = resp.json()
            latest = data.get("tag_name", "").lstrip("v") or data.get("name", "").lstrip("v")
            if not latest:
                return {
                    "current_version": current,
                    "latest_version": current,
                    "update_available": False,
                    "error": "Could not parse release version",
                }

            zip_url = ""
            for asset in data.get("assets", []):
                if asset.get("name", "").endswith(".zip"):
                    zip_url = asset.get("browser_download_url", "")
                    break
            if not zip_url:
                zip_url = data.get("zipball_url", "")

            update_available = _parse_version(latest) > _parse_version(current)

            release_notes = data.get("body", "")[:2000]

            if update_available:
                changelog = await _fetch_changelog(client)
                if changelog:
                    extracted = _extract_changelog_section(changelog, current, latest)
                    if extracted:
                        release_notes = extracted

            return {
                "current_version": current,
                "latest_version": latest,
                "update_available": update_available,
                "release_url": data.get("html_url", ""),
                "release_notes": release_notes,
                "zip_url": zip_url,
            }
    except Exception as e:
        logger.warning("Update check failed: %s", e)
        return {
            "current_version": current,
            "latest_version": current,
            "update_available": False,
            "error": str(e),
        }


async def execute_update() -> dict[str, Any]:
    """Download update zip, stage update script, and launch it.

    1. Fetch latest release info from GitHub
    2. Download zip to data/gitinthevan.zip
    3. Copy platform-specific update script to data/auto-update.{ext}
    4. Spawn it as a detached process (with 3-second delay)
    5. Return status so the HTTP response can be sent before the server dies
    """
    check = await check_for_update()
    if not check.get("update_available"):
        return {"success": False, "error": "No update available"}

    zip_url = check.get("zip_url", "")
    if not zip_url:
        return {"success": False, "error": "No download URL available for this release"}

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _DATA_DIR / "gitinthevan.zip"

    try:
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(zip_url)
            if resp.status_code != 200:
                return {"success": False, "error": f"Download failed: HTTP {resp.status_code}"}
            zip_path.write_bytes(resp.content)
    except Exception as e:
        return {"success": False, "error": f"Download failed: {e}"}

    is_windows = sys.platform == "win32"
    if is_windows:
        script_src = _SCRIPTS_DIR / "update-windows.bat"
        script_dst = _DATA_DIR / "auto-update.bat"
    else:
        script_src = _SCRIPTS_DIR / ("update-macos.sh" if sys.platform == "darwin" else "update-linux.sh")
        script_dst = _DATA_DIR / "auto-update.sh"

    if not script_src.exists():
        return {"success": False, "error": f"Update script not found: {script_src.name}"}

    shutil.copy2(script_src, script_dst)
    if not is_windows:
        script_dst.chmod(0o755)

    try:
        if is_windows:
            subprocess.Popen(
                ["cmd", "/c", str(script_dst)],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                cwd=str(_DATA_DIR.parent),
            )
        else:
            subprocess.Popen(
                ["bash", str(script_dst)],
                start_new_session=True,
                cwd=str(_DATA_DIR.parent),
            )
    except Exception as e:
        return {"success": False, "error": f"Failed to launch update script: {e}"}

    return {
        "success": True,
        "message": "Update script launched. The server will restart shortly.",
    }
