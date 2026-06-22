from __future__ import annotations

import json
import logging
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
    """Embed token in HTTPS URL for authentication."""
    if not token:
        return url
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://token:{token}@{rest}"


def fetch_repo_info(url: str, branch: str = "main", token: str = "") -> dict[str, Any]:
    """Clone a repo (shallow) and return the descriptions.json + file list.

    Uses dulwich for pure-Python git access. Repo is cloned to a temp
    directory, read, then cleaned up.
    """
    from dulwich import porcelain
    from dulwich.errors import GitProtocolError

    auth_url = _build_auth_url(url, token)

    with tempfile.TemporaryDirectory(prefix="gitv_repo_") as tmpdir:
        clone_kwargs: dict[str, Any] = {
            "checkout": True,
        }

        try:
            porcelain.clone(
                auth_url,
                target=Path(tmpdir).as_posix(),
                **clone_kwargs,
            )
        except (GitProtocolError, Exception) as e:
            err_msg = str(e)[:300]
            logger.error("Git clone failed for %s: %s", url, err_msg)
            if "not found" in err_msg.lower() or "404" in err_msg:
                raise GitCloneError(f"Repository not found: {url}") from e
            if "authentication" in err_msg.lower() or "403" in err_msg or "401" in err_msg:
                raise GitCloneError(f"Authentication failed. Check token or repo access: {url}") from e
            raise GitCloneError(f"Failed to clone repository: {err_msg}") from e

        repo_path = Path(tmpdir)

        if branch and branch != "main":
            branch_path = repo_path / ".git" / "refs" / "heads" / branch
            if not branch_path.exists():
                logger.warning("Branch '%s' not found, using default", branch)

        return _read_repo(repo_path)


def _read_repo(repo_path: Path) -> dict[str, Any]:
    """Read descriptions.json and file listing from a cloned repo."""
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
    """Scan type folders and auto-extract file metadata."""
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
    """Clone repo and read a specific file's content."""
    from dulwich import porcelain

    auth_url = _build_auth_url(url, token)

    with tempfile.TemporaryDirectory(prefix="gitv_file_") as tmpdir:
        try:
            porcelain.clone(
                auth_url,
                target=Path(tmpdir).as_posix(),
                checkout=True,
            )
        except Exception as e:
            raise GitCloneError(f"Failed to clone repository: {e}") from e

        file_full = Path(tmpdir) / file_path
        if not file_full.exists():
            raise FileNotFoundError(f"File not found in repo: {file_path}")

        return file_full.read_text(encoding="utf-8")


class GitCloneError(Exception):
    pass
