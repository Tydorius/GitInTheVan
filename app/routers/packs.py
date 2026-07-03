import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_admin
from app.models.cantrip import Cantrip
from app.models.linked_repo import InstalledItem, LinkedRepo
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.models.user import User
from app.models.verification import VerificationRule
from app.services.git_sync import GitCloneError, fetch_file_content, fetch_repo_info
from app.services.safety_scanner import scan_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/packs", tags=["content-packs"])

DISCLAIMER = (
    "Content from external repositories is not verified by GitInTheVan. "
    "Download and install at your own risk."
)

PACK_README_TEMPLATE = """# {pack_name}

This content pack was created with GitInTheVan's Pack Creator.

## Contents

{contents}

## Deploying Locally (Admin)

If you are a GitInTheVan admin, you can make this pack available to all users
via Local Folder Linking:

1. Extract this zip to a folder on the server (e.g., `data/packs/{pack_slug}/`)
2. In GitInTheVan, go to Content Packs → Link Local Folder
3. Enter the folder path
4. The pack will be visible to all users for browsing and installation

## Sharing Publicly

To share this pack with other GitInTheVan users:

1. Create a new git repository (GitHub, Gitea, etc.)
2. Extract this zip and commit the files to the repository
3. Share the repository URL — users link it via Content Packs

**Important:** Some platforms (e.g., GitHub) have content policies that may
not be friendly to NSFW content. Consider using Gitea, Forgejo, or other
self-hosted git platforms for adult content packs.
"""


def _repo_to_response(r: LinkedRepo, counts: dict[str, int]) -> dict:
    return RepoResponse(
        id=r.id,
        name=r.name,
        url=r.url,
        branch=r.branch,
        last_synced=r.last_synced.isoformat() if r.last_synced else None,
        file_count=counts.get(r.id, 0),
    ).model_dump()


async def _resolve_repo_access(repo_id: str, user_id: str, db: AsyncSession) -> LinkedRepo:
    """Find a repo that belongs to the user OR is global."""
    result = await db.execute(
        select(LinkedRepo).where(LinkedRepo.id == repo_id)
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")
    if repo.user_id != user_id and not repo.is_global:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to this repo")
    return repo


def _fetch_local_repo_info(path: str) -> dict:
    """Discover files in a local filesystem path."""
    from pathlib import Path

    repo_path = Path(path)
    if not repo_path.exists():
        raise GitCloneError(f"Local path does not exist: {path}")
    if not repo_path.is_dir():
        raise GitCloneError(f"Local path is not a directory: {path}")

    manifest_path = repo_path / "descriptions.json"
    has_manifest = manifest_path.exists()

    if has_manifest:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        manifest = {
            "pack_name": repo_path.name,
            "pack_author": "",
            "pack_version": "1.0.0",
            "pack_description": "",
            "files": [],
        }

    if not manifest.get("files"):
        manifest["files"] = _discover_local_files(repo_path)
    else:
        manifest["has_manifest"] = True

    readme = ""
    for readme_name in ("README.md", "readme.md", "README.txt"):
        readme_path = repo_path / readme_name
        if readme_path.exists():
            readme = readme_path.read_text(encoding="utf-8")
            break

    manifest["readme"] = readme
    manifest["has_manifest"] = has_manifest
    return manifest


def _discover_local_files(repo_path) -> list[dict]:
    """Auto-discover JSON files in recognized subdirectories."""
    import os

    type_folders = {
        "cantrips": "cantrip",
        "lorebooks": "lorebook",
        "rules": "rule",
        "scenario_rules": "scenario_rule",
        "skills": "skill",
        "maps": "map",
    }

    files = []
    for folder, resource_type in type_folders.items():
        dir_path = repo_path / folder
        if not dir_path.is_dir():
            continue
        for filename in sorted(os.listdir(dir_path)):
            if not filename.endswith(".json"):
                continue
            file_path = f"{folder}/{filename}"
            try:
                with open(dir_path / filename, encoding="utf-8") as f:
                    data = json.load(f)
                files.append({
                    "path": file_path,
                    "type": resource_type,
                    "name": data.get("name", filename.rsplit(".", 1)[0]),
                    "description": data.get("description", ""),
                    "author": data.get("author", ""),
                    "version": data.get("version", "1.0.0"),
                    "updated": data.get("updated", ""),
                    "tags": data.get("tags", []),
                })
            except (json.JSONDecodeError, OSError):
                pass

    return files


def _fetch_local_file_content(path: str, file_path: str) -> str:
    """Read a file from a local repo path."""
    from pathlib import Path
    full_path = Path(path) / file_path
    if not full_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    return full_path.read_text(encoding="utf-8")


class RepoLinkRequest(BaseModel):
    name: str
    url: str
    branch: str = "main"
    token: str = ""


class RepoResponse(BaseModel):
    id: str
    name: str
    url: str
    branch: str
    last_synced: str | None
    file_count: int
    is_local: bool = False
    is_global: bool = False
    can_remove: bool = True


class FileEntryResponse(BaseModel):
    path: str
    type: str
    name: str
    description: str
    author: str
    version: str
    updated: str
    tags: list[str]


class RepoBrowseResponse(BaseModel):
    pack_name: str
    pack_author: str
    pack_version: str
    pack_description: str
    files: list[FileEntryResponse]
    has_manifest: bool
    disclaimer: str
    readme: str = ""


class InstallRequest(BaseModel):
    repo_id: str
    file_path: str
    fork: bool = False


class InstalledItemResponse(BaseModel):
    id: str
    repo_id: str | None
    file_path: str
    type: str
    name: str
    description: str
    author: str
    installed_version: str
    is_fork: bool
    is_enabled: bool
    update_available: bool
    scan_result: str
    created_at: str
    updated_at: str


@router.get("/disclaimer")
async def get_disclaimer():
    return {"disclaimer": DISCLAIMER}


@router.get("/repos")
async def list_repos(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from sqlalchemy import or_

    result = await db.execute(
        select(LinkedRepo).where(
            or_(
                LinkedRepo.user_id == current_user.id,
                LinkedRepo.is_global.is_(True),
            )
        )
    )
    repos = result.scalars().all()

    items_result = await db.execute(
        select(InstalledItem).where(InstalledItem.user_id == current_user.id)
    )
    items = items_result.scalars().all()
    counts: dict[str, int] = {}
    for item in items:
        counts[item.repo_id or ""] = counts.get(item.repo_id or "", 0) + 1

    return {
        "repos": [
            {
                **_repo_to_response(r, counts),
                "is_local": r.is_local,
                "is_global": r.is_global,
                "can_remove": r.user_id == current_user.id,
            }
            for r in repos
        ],
        "disclaimer": DISCLAIMER,
    }


@router.post("/repos", status_code=status.HTTP_201_CREATED)
async def link_repo(
    req: RepoLinkRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        info = fetch_repo_info(req.url, req.branch, req.token)
    except GitCloneError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    repo = LinkedRepo(
        user_id=current_user.id,
        name=req.name,
        url=req.url,
        branch=req.branch,
        token=req.token,
        descriptions_cache=json.dumps(info),
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return RepoBrowseResponse(
        pack_name=info["pack_name"],
        pack_author=info["pack_author"],
        pack_version=info["pack_version"],
        pack_description=info["pack_description"],
        files=info["files"],
        has_manifest=info["has_manifest"],
        disclaimer=DISCLAIMER,
    ).model_dump()


class LocalRepoLinkRequest(BaseModel):
    name: str
    path: str
    is_global: bool = True


@router.post("/repos/local", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin)])
async def link_local_repo(
    req: LocalRepoLinkRequest,
    current_user: Annotated[User, Depends(require_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    import os
    if not os.path.isdir(req.path):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Path does not exist or is not a directory: {req.path}")

    try:
        info = _fetch_local_repo_info(req.path)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    repo = LinkedRepo(
        user_id=current_user.id,
        name=req.name,
        url=req.path,
        branch="local",
        token="",
        is_local=True,
        is_global=req.is_global,
        descriptions_cache=json.dumps(info),
    )
    db.add(repo)
    await db.commit()
    await db.refresh(repo)

    return RepoBrowseResponse(
        pack_name=info["pack_name"],
        pack_author=info["pack_author"],
        pack_version=info["pack_version"],
        pack_description=info["pack_description"],
        files=info["files"],
        has_manifest=info["has_manifest"],
        disclaimer=DISCLAIMER,
        readme=info.get("readme", ""),
    ).model_dump()


@router.post("/repos/{repo_id}/sync")
async def sync_repo(
    repo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from datetime import UTC, datetime

    repo = await _resolve_repo_access(repo_id, current_user.id, db)

    try:
        if repo.is_local:
            info = _fetch_local_repo_info(repo.url)
        else:
            info = fetch_repo_info(repo.url, repo.branch, repo.token)
    except GitCloneError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    repo.descriptions_cache = json.dumps(info)
    repo.last_synced = datetime.now(UTC)
    await db.commit()

    return RepoBrowseResponse(
        pack_name=info["pack_name"],
        pack_author=info["pack_author"],
        pack_version=info["pack_version"],
        pack_description=info["pack_description"],
        files=info["files"],
        has_manifest=info["has_manifest"],
        disclaimer=DISCLAIMER,
        readme=info.get("readme", ""),
    ).model_dump()


@router.get("/repos/{repo_id}/browse")
async def browse_repo(
    repo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    repo = await _resolve_repo_access(repo_id, current_user.id, db)

    if repo.descriptions_cache:
        info = json.loads(repo.descriptions_cache)
    else:
        try:
            if repo.is_local:
                info = _fetch_local_repo_info(repo.url)
            else:
                info = fetch_repo_info(repo.url, repo.branch, repo.token)
            repo.descriptions_cache = json.dumps(info)
            await db.commit()
        except GitCloneError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return RepoBrowseResponse(
        pack_name=info["pack_name"],
        pack_author=info["pack_author"],
        pack_version=info["pack_version"],
        pack_description=info["pack_description"],
        files=info["files"],
        has_manifest=info["has_manifest"],
        disclaimer=DISCLAIMER,
        readme=info.get("readme", ""),
    ).model_dump()


@router.delete("/repos/{repo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repo(
    repo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(LinkedRepo).where(
            LinkedRepo.id == repo_id, LinkedRepo.user_id == current_user.id
        )
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found or you do not have permission to remove it")
    await db.delete(repo)
    await db.commit()


@router.post("/install", status_code=status.HTTP_201_CREATED)
async def install_file(
    req: InstallRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    repo = await _resolve_repo_access(req.repo_id, current_user.id, db)

    try:
        if repo.is_local:
            raw_content = _fetch_local_file_content(repo.url, req.file_path)
        else:
            raw_content = fetch_file_content(repo.url, req.file_path, repo.token)
    except GitCloneError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    resource_type = req.file_path.split("/")[0].rstrip("s")
    if resource_type == "rule":
        resource_type = "rule"
    elif resource_type == "cantrip":
        resource_type = "cantrip"
    elif resource_type == "lorebook":
        resource_type = "lorebook"
    elif resource_type == "map":
        resource_type = "map"
    elif resource_type == "scenario_rule":
        resource_type = "scenario_rule"

    scan_result = scan_file(raw_content, resource_type)

    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File is not valid JSON")

    local_id = await _create_local_resource(db, current_user.id, resource_type, data)

    scan_summary = json.dumps({
        "safe": scan_result.safe,
        "max_severity": scan_result.max_severity,
        "findings": [
            {"severity": f.severity, "description": f.description, "line": f.line}
            for f in scan_result.findings
        ],
    })

    item = InstalledItem(
        user_id=current_user.id,
        repo_id=None if req.fork else repo.id,
        file_path=req.file_path,
        type=resource_type,
        name=data.get("name", req.file_path),
        description=data.get("description", ""),
        author=data.get("author", ""),
        installed_version=data.get("version", "1.0.0"),
        installed_commit="",
        local_id=local_id,
        is_fork=req.fork,
        is_enabled=False,
        scan_result=scan_summary,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)

    return {
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "scan": {
            "safe": scan_result.safe,
            "max_severity": scan_result.max_severity,
            "findings": [
                {"severity": f.severity, "description": f.description}
                for f in scan_result.findings
            ],
        },
        "disclaimer": DISCLAIMER,
    }


@router.get("/installed")
async def list_installed(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(InstalledItem).where(InstalledItem.user_id == current_user.id)
    )
    items = result.scalars().all()
    return {
        "items": [
            InstalledItemResponse(
                id=i.id,
                repo_id=i.repo_id,
                file_path=i.file_path,
                type=i.type,
                name=i.name,
                description=i.description,
                author=i.author,
                installed_version=i.installed_version,
                is_fork=i.is_fork,
                is_enabled=i.is_enabled,
                update_available=i.update_available,
                scan_result=i.scan_result,
                created_at=i.created_at.isoformat() if i.created_at else "",
                updated_at=i.updated_at.isoformat() if i.updated_at else "",
            ).model_dump()
            for i in items
        ],
        "disclaimer": DISCLAIMER,
    }


@router.put("/installed/{item_id}/toggle")
async def toggle_installed(
    item_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.id == item_id, InstalledItem.user_id == current_user.id
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    item.is_enabled = not item.is_enabled

    if item.local_id:
        await _toggle_local_resource(db, item.type, item.local_id, item.is_enabled)

    await db.commit()
    return {"is_enabled": item.is_enabled}


@router.delete("/installed/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def uninstall_item(
    item_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.id == item_id, InstalledItem.user_id == current_user.id
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    if item.local_id:
        await _delete_local_resource(db, item.type, item.local_id)

    await db.delete(item)
    await db.commit()


@router.post("/repos/{repo_id}/check-updates")
async def check_updates(
    repo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Check if any installed items from this repo have updates available."""
    from app.services.git_sync import check_for_updates as do_check

    repo = await _resolve_repo_access(repo_id, current_user.id, db)

    items_result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.user_id == current_user.id,
            InstalledItem.repo_id == repo_id,
            InstalledItem.is_fork.is_(False),
        )
    )
    items = items_result.scalars().all()

    if not items:
        return {"checked": 0, "updates_available": 0, "results": []}

    item_dicts = [
        {"file_path": i.file_path, "installed_version": i.installed_version, "name": i.name}
        for i in items
    ]

    results = do_check(repo.url, repo.branch, repo.token, item_dicts)

    update_count = 0
    for item, result_data in zip(items, results, strict=False):
        item.update_available = result_data["has_update"]
        if result_data["has_update"]:
            update_count += 1

    await db.commit()

    return {
        "checked": len(items),
        "updates_available": update_count,
        "results": results,
    }


class CreatePackRequest(BaseModel):
    pack_name: str = "My Content Pack"
    pack_author: str = ""
    pack_description: str = ""
    resources: list[dict] = []


@router.post("/create")
async def create_pack(
    req: CreatePackRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    import io
    import zipfile

    from fastapi.responses import StreamingResponse

    if not req.resources:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one resource")

    serialized_files: list[tuple[str, bytes]] = []
    manifest_files: list[dict] = []
    contents_summary: list[str] = []

    for resource in req.resources:
        rtype = resource.get("type", "")
        rid = resource.get("id", "")
        data, file_path, manifest_entry = await _serialize_resource(db, current_user.id, rtype, rid)
        if data is None:
            continue

        file_bytes = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        serialized_files.append((file_path, file_bytes))
        manifest_files.append(manifest_entry)
        contents_summary.append(f"- {manifest_entry['type']}: {manifest_entry['name']} ({file_path})")

    pack_slug = req.pack_name.lower().replace(" ", "-").replace("/", "-")[:64] or "content-pack"
    manifest = {
        "pack_name": req.pack_name,
        "pack_author": req.pack_author,
        "pack_version": "1.0.0",
        "pack_description": req.pack_description,
        "pack_url": "",
        "files": manifest_files,
    }

    manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
    serialized_files.append(("descriptions.json", manifest_bytes))

    readme_text = PACK_README_TEMPLATE.format(
        pack_name=req.pack_name,
        pack_slug=pack_slug,
        contents="\n".join(contents_summary) or "(no resources)",
    )
    serialized_files.append(("README.md", readme_text.encode("utf-8")))

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in serialized_files:
            zf.writestr(path, data)

    zip_buffer.seek(0)
    filename = f"{pack_slug}.zip"

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _serialize_resource(
    db: AsyncSession, user_id: str, rtype: str, rid: str
) -> tuple[dict | None, str, dict]:
    """Serialize a resource to JSON, returning (data, file_path, manifest_entry)."""

    if rtype == "cantrip":
        result = await db.execute(select(Cantrip).where(Cantrip.id == rid, Cantrip.user_id == user_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        data = {
            "name": obj.name, "description": obj.description or "",
            "llm_instructions": obj.llm_instructions or "",
            "code": obj.code or "",
            "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"cantrips/{slug}.json", {"path": f"cantrips/{slug}.json", "type": "cantrip", **data}

    if rtype == "lorebook":
        result = await db.execute(
            select(Lorebook).where(Lorebook.id == rid, Lorebook.user_id == user_id)
        )
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        entries_result = await db.execute(
            select(LorebookEntry).where(LorebookEntry.lorebook_id == rid)
        )
        entries = entries_result.scalars().all()
        entry_list = []
        for e in entries:
            entry_list.append({
                "name": e.name, "keys": json.loads(e.keys) if e.keys else [],
                "secondary_keys": json.loads(e.secondary_keys) if e.secondary_keys else [],
                "content": e.content, "position": e.position,
                "insertion_order": e.insertion_order,
                "is_constant": e.is_constant, "is_selective": e.is_selective,
                "is_disabled": e.is_disabled,
            })
        data = {
            "name": obj.name, "description": obj.description or "",
            "entries": entry_list, "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"lorebooks/{slug}.json", {"path": f"lorebooks/{slug}.json", "type": "lorebook", **data}

    if rtype == "skill":
        from app.models.skill import Skill
        result = await db.execute(select(Skill).where(Skill.id == rid, Skill.user_id == user_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        data = {
            "name": obj.name, "description": obj.description or "",
            "content": obj.content or "", "type": obj.type,
            "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"skills/{slug}.json", {"path": f"skills/{slug}.json", "type": "skill", **data}

    if rtype == "scenario_rule":
        from app.models.scenario_rule import ScenarioRule
        result = await db.execute(select(ScenarioRule).where(ScenarioRule.id == rid, ScenarioRule.user_id == user_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        data = {
            "name": obj.name, "description": "",
            "token_threshold": obj.token_threshold,
            "fire_position": obj.fire_position,
            "model": obj.model or "", "prompt": obj.prompt or "",
            "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"scenario_rules/{slug}.json", {"path": f"scenario_rules/{slug}.json", "type": "scenario_rule", **data}

    if rtype == "rule":
        result = await db.execute(select(VerificationRule).where(VerificationRule.id == rid, VerificationRule.user_id == user_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        data = {
            "name": obj.name, "description": obj.description or "",
            "prompt": obj.prompt or "",
            "max_retries": obj.max_retries,
            "execution_order": obj.execution_order,
            "resubmission_strategy": obj.resubmission_strategy,
            "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"rules/{slug}.json", {"path": f"rules/{slug}.json", "type": "rule", **data}

    if rtype == "map":
        from app.models.map import Map, MapStage
        result = await db.execute(select(Map).where(Map.id == rid, Map.user_id == user_id))
        obj = result.scalar_one_or_none()
        if not obj:
            return None, "", {}
        stages_result = await db.execute(select(MapStage).where(MapStage.map_id == rid))
        stages = stages_result.scalars().all()
        stage_list = []
        for s in stages:
            stage_list.append({
                "name": s.name, "system_instructions": s.system_instructions or "",
                "endpoint_id": s.endpoint_id, "model_override": s.model_override or "",
                "driver_callable_turns": s.driver_callable_turns,
                "output_mode": s.output_mode,
            })
        data = {
            "name": obj.name, "description": "",
            "stages": stage_list, "author": "", "version": "1.0.0",
            "updated": obj.created_at.isoformat() if obj.created_at else "",
        }
        slug = obj.name.lower().replace(" ", "_")[:64]
        return data, f"maps/{slug}.json", {"path": f"maps/{slug}.json", "type": "map", **data}

    return None, "", {}


async def _create_local_resource(
    db: AsyncSession, user_id: str, resource_type: str, data: dict
) -> str:
    """Create a local resource from pack data. Always disabled."""
    if resource_type == "cantrip":
        cantrip = Cantrip(
            user_id=user_id,
            name=data.get("name", "Imported"),
            description=data.get("description", ""),
            llm_instructions=data.get("llm_instructions", ""),
            code=data.get("code", ""),
            is_active=False,
            is_public=False,
        )
        db.add(cantrip)
        await db.flush()
        return cantrip.id

    if resource_type == "lorebook":
        lb = Lorebook(
            user_id=user_id,
            name=data.get("name", "Imported"),
            description=data.get("description", ""),
            is_active=False,
        )
        db.add(lb)
        await db.flush()

        entries = data.get("entries", [])
        if isinstance(entries, dict):
            entries = list(entries.values())
        for entry_data in entries:
            import json as _json
            entry = LorebookEntry(
                lorebook_id=lb.id,
                name=entry_data.get("name", ""),
                keys=_json.dumps(entry_data.get("keys", [])),
                secondary_keys=_json.dumps(entry_data.get("secondary_keys", [])),
                content=entry_data.get("content", ""),
                position=entry_data.get("position", "before_last_message"),
                insertion_order=entry_data.get("insertion_order", 10),
                is_constant=entry_data.get("is_constant", False),
                is_selective=entry_data.get("is_selective", False),
                is_disabled=entry_data.get("is_disabled", False),
            )
            db.add(entry)

        return lb.id

    if resource_type == "rule":
        rule = VerificationRule(
            user_id=user_id,
            name=data.get("name", "Imported"),
            description=data.get("description", ""),
            prompt=data.get("prompt", ""),
            is_active=False,
        )
        db.add(rule)
        await db.flush()
        return rule.id

    if resource_type == "scenario_rule":
        from app.models.scenario_rule import ScenarioRule
        sr = ScenarioRule(
            user_id=user_id,
            name=data.get("name", "Imported"),
            token_threshold=data.get("token_threshold", 2000),
            fire_position=data.get("fire_position", "pre"),
            model=data.get("model", ""),
            prompt=data.get("prompt", ""),
            is_active=False,
        )
        db.add(sr)
        await db.flush()
        return sr.id

    return ""


async def _toggle_local_resource(
    db: AsyncSession, resource_type: str, local_id: str, enabled: bool
):
    if resource_type == "cantrip":
        result = await db.execute(select(Cantrip).where(Cantrip.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            obj.is_active = enabled
    elif resource_type == "lorebook":
        result = await db.execute(select(Lorebook).where(Lorebook.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            obj.is_active = enabled
    elif resource_type == "rule":
        result = await db.execute(select(VerificationRule).where(VerificationRule.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            obj.is_active = enabled
    elif resource_type == "scenario_rule":
        from app.models.scenario_rule import ScenarioRule
        result = await db.execute(select(ScenarioRule).where(ScenarioRule.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            obj.is_active = enabled


async def _delete_local_resource(
    db: AsyncSession, resource_type: str, local_id: str
):
    if resource_type == "cantrip":
        result = await db.execute(select(Cantrip).where(Cantrip.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            await db.delete(obj)
    elif resource_type == "lorebook":
        result = await db.execute(select(Lorebook).where(Lorebook.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            await db.delete(obj)
    elif resource_type == "rule":
        result = await db.execute(select(VerificationRule).where(VerificationRule.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            await db.delete(obj)
    elif resource_type == "scenario_rule":
        from app.models.scenario_rule import ScenarioRule
        result = await db.execute(select(ScenarioRule).where(ScenarioRule.id == local_id))
        obj = result.scalar_one_or_none()
        if obj:
            await db.delete(obj)
