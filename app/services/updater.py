"""Check for GitInTheVan updates from GitHub releases."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/Tydorius/GitInTheVan/releases/latest"


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

        return {
            "current_version": current,
            "latest_version": latest,
            "update_available": _parse_version(latest) > _parse_version(current),
            "release_url": data.get("html_url", ""),
            "release_notes": data.get("body", "")[:2000],
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
