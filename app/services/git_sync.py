from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RepoFileEntry:
    path: str
    type: str
    name: str
    description: str
    author: str
    version: str
    updated: str
    tags: list[str]
    min_gitv_version: str


@dataclass
class RepoManifest:
    pack_name: str
    pack_author: str
    pack_version: str
    pack_description: str
    files: list[RepoFileEntry]
    raw: dict


def _build_auth_url(url: str, token: str) -> str:
    if not token:
        return url
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://token:{token}@{rest}"


class _WinTempDir:
    """Temp directory that suppresses Windows file-lock cleanup errors."""

    def __init__(self):
        self.path = Path(tempfile.mkdtemp(prefix="gitv_repo_"))

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        shutil.rmtree(self.path, ignore_errors=True)
        return False


def fetch_repo_info(url: str, branch: str = "main", token: str = "") -> dict[str, Any]:
    from dulwich import porcelain

    auth_url = _build_auth_url(url, token)

    with _WinTempDir() as tmpdir:
        try:
            porcelain.clone(
                auth_url,
                target=str(tmpdir),
                checkout=True,
            )
        except Exception as e:
            err_msg = str(e)[:300]
            logger.error("Git clone failed for %s: %s", url, err_msg)
            if "not found" in err_msg.lower() or "404" in err_msg:
                raise GitCloneError(f"Repository not found: {url}") from e
            if "authentication" in err_msg.lower() or "403" in err_msg or "401" in err_msg:
                raise GitCloneError(f"Authentication failed. Check token or repo access: {url}") from e
            raise GitCloneError(f"Failed to clone repository: {err_msg}") from e

        return _read_repo(tmpdir)


def _read_repo(repo_path: Path) -> dict[str, Any]:
    manifest_path = repo_path / "descriptions.json"

    files: list[dict[str, Any]] = []
    manifest_data: dict[str, Any] = {}

    if manifest_path.exists():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest_data.get("files", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read descriptions.json: %s", e)

    if not files:
        files = _auto_scan_repo(repo_path)

    return {
        "pack_name": manifest_data.get("pack_name", repo_path.name),
        "pack_author": manifest_data.get("pack_author", ""),
        "pack_version": manifest_data.get("pack_version", "1.0.0"),
        "pack_description": manifest_data.get("pack_description", ""),
        "files": files,
        "has_manifest": manifest_path.exists(),
    }


def _auto_scan_repo(repo_path: Path) -> list[dict[str, Any]]:
    type_folders = {
        "cantrips": "cantrip",
        "lorebooks": "lorebook",
        "rules": "rule",
        "maps": "map",
    }

    files: list[dict[str, Any]] = []

    for folder_name, resource_type in type_folders.items():
        folder = repo_path / folder_name
        if not folder.is_dir():
            continue

        for json_file in sorted(folder.glob("*.json")):
            try:
                data = json.loads(json_file.read_text(encoding="utf-8"))
                files.append({
                    "path": f"{folder_name}/{json_file.name}",
                    "type": resource_type,
                    "name": data.get("name", json_file.stem),
                    "description": data.get("description", ""),
                    "author": data.get("author", ""),
                    "version": data.get("version", "1.0.0"),
                    "updated": "",
                    "tags": [],
                    "min_gitv_version": "",
                    "_auto_generated": True,
                })
            except (json.JSONDecodeError, OSError):
                continue

    return files


def fetch_file_content(url: str, file_path: str, token: str = "") -> str:
    from dulwich import porcelain

    auth_url = _build_auth_url(url, token)

    with _WinTempDir() as tmpdir:
        try:
            porcelain.clone(
                auth_url,
                target=str(tmpdir),
                checkout=True,
            )
        except Exception as e:
            raise GitCloneError(f"Failed to clone repository: {e}") from e

        file_full = tmpdir / file_path
        if not file_full.exists():
            raise FileNotFoundError(f"File not found in repo: {file_path}")

        return file_full.read_text(encoding="utf-8")


class GitCloneError(Exception):
    pass
