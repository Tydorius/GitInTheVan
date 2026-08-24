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
    readme_content = ""

    if manifest_path.exists():
        try:
            manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
            files = manifest_data.get("files", [])
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read descriptions.json: %s", e)

    if not files:
        files = _auto_scan_repo(repo_path)

    for readme_name in ["README.md", "readme.md", "README", "readme.txt", "README.txt"]:
        readme_path = repo_path / readme_name
        if readme_path.exists():
            try:
                readme_content = readme_path.read_text(encoding="utf-8")[:10000]
            except OSError:
                pass
            break

    return {
        "pack_name": manifest_data.get("pack_name", repo_path.name),
        "pack_author": manifest_data.get("pack_author", ""),
        "pack_version": manifest_data.get("pack_version", "1.0.0"),
        "pack_description": manifest_data.get("pack_description", ""),
        "files": files,
        "has_manifest": manifest_path.exists(),
        "readme": readme_content,
    }


def check_for_updates(
    repo_url: str,
    branch: str,
    token: str,
    installed_items: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Check if installed items have updates available.

    Re-fetches repo info and compares file paths/versions.
    Returns list of {file_path, has_update, new_version, current_version}.
    """
    try:
        repo_info = fetch_repo_info(repo_url, branch, token)
    except GitCloneError:
        return []

    repo_files = {f["path"]: f for f in repo_info.get("files", [])}
    results = []

    from app.services.resource_identity import parse_resource_path

    for item in installed_items:
        file_path = item.get("file_path", "")
        current_version = item.get("installed_version", "")
        result = {"file_path": file_path, "has_update": False, "new_version": "", "current_version": current_version}

        # A resource that a pack ships only inside a map is recorded as
        # "maps/x.json:Name". The file that can have an update is the map.
        lookup_path, _key = parse_resource_path(file_path)

        if lookup_path in repo_files:
            repo_file = repo_files[lookup_path]
            new_version = repo_file.get("version", "")
            result["new_version"] = new_version
            if new_version and new_version != current_version:
                result["has_update"] = True
            elif not new_version and current_version:
                pass
            elif not _key:
                repo_name = repo_file.get("name", "")
                installed_name = item.get("name", "")
                if repo_name and installed_name and repo_name != installed_name:
                    result["has_update"] = True
        else:
            result["has_update"] = False
            result["new_version"] = "(removed from repo)"

        results.append(result)

    return results


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


class UnsafeRepoPathError(ValueError):
    """A resource path tried to address something outside the repo."""


def safe_repo_join(root: Path | str, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside ``root``, refusing anything that escapes.

    ``Path(root) / rel_path`` is NOT safe on its own: an absolute right-hand
    operand replaces the left entirely (``Path("C:/repo") / "C:/Windows/x"`` is
    ``C:/Windows/x``), and ``..`` walks out. Both reach caller-supplied values,
    so every repo-relative path goes through here.

    Also refuses anything outside the folders a pack publishes resources from --
    a resource reference has no business addressing arbitrary repo files.
    """
    from app.services.resource_identity import RESOURCE_FOLDERS

    rel = (rel_path or "").strip().replace("\\", "/")
    if not rel:
        raise UnsafeRepoPathError("Empty resource path")

    candidate = Path(rel)
    if candidate.is_absolute() or candidate.drive or rel.startswith("/"):
        raise UnsafeRepoPathError(f"Absolute paths are not allowed: {rel_path}")
    if ".." in candidate.parts:
        raise UnsafeRepoPathError(f"Path escapes the repository: {rel_path}")

    top = candidate.parts[0] if candidate.parts else ""
    if top not in RESOURCE_FOLDERS:
        raise UnsafeRepoPathError(
            f"Resource paths must start with one of {', '.join(RESOURCE_FOLDERS)}: {rel_path}"
        )

    root_resolved = Path(root).resolve()
    full = (root_resolved / candidate).resolve()
    # Belt and braces: symlinks inside the repo can still point outward.
    if not full.is_relative_to(root_resolved):
        raise UnsafeRepoPathError(f"Path escapes the repository: {rel_path}")

    return full


def fetch_files_content(
    url: str, file_paths: list[str], token: str = ""
) -> dict[str, str]:
    """Read several files from a repo with a single clone.

    Cloning once per file made a map with five linked resources five full
    clones. Returns ``{file_path: content}``; a path missing from the repo is
    omitted rather than raising, so one bad reference does not lose the rest.
    """
    from dulwich import porcelain

    if not file_paths:
        return {}

    auth_url = _build_auth_url(url, token)

    with _WinTempDir() as tmpdir:
        try:
            porcelain.clone(auth_url, target=str(tmpdir), checkout=True)
        except Exception as e:
            raise GitCloneError(f"Failed to clone repository: {e}") from e

        out: dict[str, str] = {}
        for file_path in file_paths:
            try:
                full = safe_repo_join(tmpdir, file_path)
            except UnsafeRepoPathError:
                logger.warning("Refusing unsafe repo path: %s", file_path)
                continue
            if full.is_file():
                out[file_path] = full.read_text(encoding="utf-8")
        return out


def fetch_file_content(url: str, file_path: str, token: str = "") -> str:
    contents = fetch_files_content(url, [file_path], token)
    if file_path not in contents:
        raise FileNotFoundError(f"File not found in repo: {file_path}")
    return contents[file_path]


class GitCloneError(Exception):
    pass
