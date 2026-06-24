import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
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
    result = await db.execute(
        select(LinkedRepo).where(LinkedRepo.user_id == current_user.id)
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
            RepoResponse(
                id=r.id,
                name=r.name,
                url=r.url,
                branch=r.branch,
                last_synced=r.last_synced.isoformat() if r.last_synced else None,
                file_count=counts.get(r.id, 0),
            ).model_dump()
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


@router.post("/repos/{repo_id}/sync")
async def sync_repo(
    repo_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from datetime import UTC, datetime

    result = await db.execute(
        select(LinkedRepo).where(
            LinkedRepo.id == repo_id, LinkedRepo.user_id == current_user.id
        )
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    try:
        info = fetch_repo_info(repo.url, repo.branch, repo.token)
    except GitCloneError as e:
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
    result = await db.execute(
        select(LinkedRepo).where(
            LinkedRepo.id == repo_id, LinkedRepo.user_id == current_user.id
        )
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    if repo.descriptions_cache:
        info = json.loads(repo.descriptions_cache)
    else:
        try:
            info = fetch_repo_info(repo.url, repo.branch, repo.token)
            repo.descriptions_cache = json.dumps(info)
            await db.commit()
        except GitCloneError as e:
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")
    await db.delete(repo)
    await db.commit()


@router.post("/install", status_code=status.HTTP_201_CREATED)
async def install_file(
    req: InstallRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(LinkedRepo).where(
            LinkedRepo.id == req.repo_id, LinkedRepo.user_id == current_user.id
        )
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

    try:
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

    result = await db.execute(
        select(LinkedRepo).where(
            LinkedRepo.id == repo_id, LinkedRepo.user_id == current_user.id
        )
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repo not found")

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
