import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.dependencies import get_current_user
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/lorebooks", tags=["lorebooks"])


class EntryInput(BaseModel):
    name: str = ""
    keys: list[str] = []
    secondary_keys: list[str] = []
    content: str = ""
    content_summary: str = ""
    content_bullets: str = ""
    position: str = "before_last_message"
    insertion_order: int = 10
    is_constant: bool = False
    is_selective: bool = False
    is_disabled: bool = False
    character_limit: int = 0


class EntryResponse(BaseModel):
    id: str
    name: str
    keys: list[str]
    secondary_keys: list[str]
    content: str
    content_summary: str
    content_bullets: str
    position: str
    insertion_order: int
    is_constant: bool
    is_selective: bool
    is_disabled: bool
    character_limit: int


class LorebookCreate(BaseModel):
    name: str
    description: str = ""
    is_public: bool = False
    is_active: bool = True
    tag: str = ""


class LorebookUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    tag: str | None = None


class LorebookResponse(BaseModel):
    id: str
    name: str
    description: str
    is_public: bool
    is_active: bool
    tag: str
    entries: list[EntryResponse]


class LorebookListItem(BaseModel):
    id: str
    name: str
    description: str
    is_public: bool
    is_active: bool
    tag: str
    entry_count: int


class LorebookListResponse(BaseModel):
    lorebooks: list[LorebookListItem]


class ExportData(BaseModel):
    name: str
    description: str
    entries: list[EntryInput]


class RawImport(BaseModel):
    name: str | None = None
    description: str | None = None
    entries: Any = None


def _entry_to_response(entry: LorebookEntry) -> EntryResponse:
    return EntryResponse(
        id=entry.id,
        name=entry.name,
        keys=json.loads(entry.keys) if entry.keys else [],
        secondary_keys=json.loads(entry.secondary_keys) if entry.secondary_keys else [],
        content=entry.content,
        content_summary=entry.content_summary,
        content_bullets=entry.content_bullets,
        position=entry.position,
        insertion_order=entry.insertion_order,
        is_constant=entry.is_constant,
        is_selective=entry.is_selective,
        is_disabled=entry.is_disabled,
        character_limit=entry.character_limit,
    )


def _entry_input_to_dict(data: EntryInput) -> dict:
    return {
        "name": data.name,
        "keys": json.dumps(data.keys),
        "secondary_keys": json.dumps(data.secondary_keys),
        "content": data.content,
        "content_summary": data.content_summary,
        "content_bullets": data.content_bullets,
        "position": data.position,
        "insertion_order": data.insertion_order,
        "is_constant": data.is_constant,
        "is_selective": data.is_selective,
        "is_disabled": data.is_disabled,
        "character_limit": data.character_limit,
    }


@router.get("")
async def list_lorebooks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.user_id == current_user.id)
        .options(selectinload(Lorebook.entries))
        .order_by(Lorebook.created_at)
    )
    lorebooks = result.scalars().all()
    return LorebookListResponse(
        lorebooks=[
            LorebookListItem(
                id=lb.id,
                name=lb.name,
                description=lb.description,
                is_public=lb.is_public,
                is_active=lb.is_active,
                tag=lb.tag,
                entry_count=len(lb.entries),
            )
            for lb in lorebooks
        ]
    )


@router.get("/public")
async def list_public_lorebooks(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.is_public.is_(True))
        .options(selectinload(Lorebook.entries))
        .order_by(Lorebook.updated_at.desc())
    )
    lorebooks = result.scalars().all()
    return LorebookListResponse(
        lorebooks=[
            LorebookListItem(
                id=lb.id,
                name=lb.name,
                description=lb.description,
                is_public=lb.is_public,
                is_active=lb.is_active,
                tag=lb.tag,
                entry_count=len(lb.entries),
            )
            for lb in lorebooks
        ]
    )


@router.get("/{lorebook_id}")
async def get_lorebook(
    lorebook_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.id == lorebook_id)
        .options(selectinload(Lorebook.entries))
    )
    lorebook = result.scalar_one_or_none()
    if lorebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")
    if lorebook.user_id != current_user.id and not lorebook.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return LorebookResponse(
        id=lorebook.id,
        name=lorebook.name,
        description=lorebook.description,
        is_public=lorebook.is_public,
        is_active=lorebook.is_active,
        tag=lorebook.tag,
        entries=[_entry_to_response(e) for e in lorebook.entries],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_lorebook(
    req: LorebookCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    lorebook = Lorebook(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        is_public=req.is_public,
        is_active=req.is_active,
        tag=req.tag,
    )
    db.add(lorebook)
    await db.commit()
    await db.refresh(lorebook)
    return LorebookResponse(
        id=lorebook.id,
        name=lorebook.name,
        description=lorebook.description,
        is_public=lorebook.is_public,
        is_active=lorebook.is_active,
        tag=lorebook.tag,
        entries=[],
    )


@router.put("/{lorebook_id}")
async def update_lorebook(
    lorebook_id: str,
    req: LorebookUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook).where(Lorebook.id == lorebook_id, Lorebook.user_id == current_user.id)
    )
    lorebook = result.scalar_one_or_none()
    if lorebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")

    if req.name is not None:
        lorebook.name = req.name
    if req.description is not None:
        lorebook.description = req.description
    if req.is_public is not None:
        lorebook.is_public = req.is_public
    if req.is_active is not None:
        lorebook.is_active = req.is_active
    if req.tag is not None and req.tag != lorebook.tag:
        from sqlalchemy import text as sa_text
        existing = await db.execute(
            sa_text("SELECT id FROM lorebooks WHERE tag = :tag AND user_id = :uid AND id != :lid"),
            {"tag": req.tag, "uid": current_user.id, "lid": lorebook_id}
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already in use")
        lorebook.tag = req.tag

    await db.commit()
    await db.refresh(lorebook)
    return LorebookResponse(
        id=lorebook.id,
        name=lorebook.name,
        description=lorebook.description,
        is_public=lorebook.is_public,
        is_active=lorebook.is_active,
        tag=lorebook.tag,
        entries=[],
    )


@router.delete("/{lorebook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lorebook(
    lorebook_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook).where(Lorebook.id == lorebook_id, Lorebook.user_id == current_user.id)
    )
    lorebook = result.scalar_one_or_none()
    if lorebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")
    await db.delete(lorebook)
    await db.commit()


@router.post("/{lorebook_id}/entries", status_code=status.HTTP_201_CREATED)
async def add_entry(
    lorebook_id: str,
    req: EntryInput,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook).where(Lorebook.id == lorebook_id, Lorebook.user_id == current_user.id)
    )
    lorebook = result.scalar_one_or_none()
    if lorebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")

    entry = LorebookEntry(lorebook_id=lorebook_id, **_entry_input_to_dict(req))
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return _entry_to_response(entry)


@router.put("/{lorebook_id}/entries/{entry_id}")
async def update_entry(
    lorebook_id: str,
    entry_id: str,
    req: EntryInput,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    lb_result = await db.execute(
        select(Lorebook).where(Lorebook.id == lorebook_id, Lorebook.user_id == current_user.id)
    )
    if lb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")

    result = await db.execute(
        select(LorebookEntry).where(LorebookEntry.id == entry_id, LorebookEntry.lorebook_id == lorebook_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

    for key, value in _entry_input_to_dict(req).items():
        setattr(entry, key, value)

    await db.commit()
    await db.refresh(entry)
    return _entry_to_response(entry)


@router.delete("/{lorebook_id}/entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    lorebook_id: str,
    entry_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    lb_result = await db.execute(
        select(Lorebook).where(Lorebook.id == lorebook_id, Lorebook.user_id == current_user.id)
    )
    if lb_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")

    result = await db.execute(
        select(LorebookEntry).where(LorebookEntry.id == entry_id, LorebookEntry.lorebook_id == lorebook_id)
    )
    entry = result.scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entry not found")

    await db.delete(entry)
    await db.commit()


@router.get("/{lorebook_id}/export")
async def export_lorebook(
    lorebook_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.id == lorebook_id)
        .options(selectinload(Lorebook.entries))
    )
    lorebook = result.scalar_one_or_none()
    if lorebook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lorebook not found")
    if lorebook.user_id != current_user.id and not lorebook.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    entries = []
    for e in lorebook.entries:
        entries.append(
            EntryInput(
                name=e.name,
                keys=json.loads(e.keys) if e.keys else [],
                secondary_keys=json.loads(e.secondary_keys) if e.secondary_keys else [],
                content=e.content,
                content_summary=e.content_summary,
                content_bullets=e.content_bullets,
                position=e.position,
                insertion_order=e.insertion_order,
                is_constant=e.is_constant,
                is_selective=e.is_selective,
                is_disabled=e.is_disabled,
                character_limit=e.character_limit,
            )
        )

    return ExportData(
        name=lorebook.name,
        description=lorebook.description,
        entries=entries,
    )


def _normalize_entry(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return _entry_input_to_dict(EntryInput())

    keys = raw.get("keys") or raw.get("key") or []
    if not isinstance(keys, list):
        keys = [keys] if keys else []
    keys = [str(k).strip() for k in keys if str(k).strip()]

    sec_keys = raw.get("secondary_keys") or raw.get("keysecondary") or []
    if not isinstance(sec_keys, list):
        sec_keys = [sec_keys] if sec_keys else []
    sec_keys = [str(k).strip() for k in sec_keys if str(k).strip()]

    position = raw.get("position", "before_last_message")
    if position == 1 or position == "1":
        position = "before_last_message"
    elif position == 0 or position == "0":
        position = "system_start"

    return {
        "name": str(raw.get("name") or raw.get("comment") or ""),
        "keys": json.dumps(keys),
        "secondary_keys": json.dumps(sec_keys),
        "content": str(raw.get("content") or ""),
        "content_summary": "",
        "content_bullets": "",
        "position": position,
        "insertion_order": int(raw.get("insertion_order") or raw.get("order") or raw.get("priority") or 10),
        "is_constant": bool(raw.get("constant") or raw.get("is_constant") or False),
        "is_selective": bool(raw.get("selective") or raw.get("is_selective") or False),
        "is_disabled": bool(raw.get("disable") or raw.get("is_disabled") or not (raw.get("enabled", True))),
        "character_limit": int(raw.get("character_limit") or 0),
    }


@router.post("/import", status_code=status.HTTP_201_CREATED)
async def import_lorebook(
    req: RawImport,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    raw_entries = req.entries
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    if not isinstance(raw_entries, list):
        raw_entries = []

    normalized = [_normalize_entry(e) for e in raw_entries if isinstance(e, dict)]

    lorebook = Lorebook(
        user_id=current_user.id,
        name=req.name or "Imported Lorebook",
        description=req.description or "",
    )
    db.add(lorebook)
    await db.flush()

    for entry_dict in normalized:
        entry = LorebookEntry(lorebook_id=lorebook.id, **entry_dict)
        db.add(entry)

    await db.commit()
    await db.refresh(lorebook)

    result = await db.execute(
        select(Lorebook)
        .where(Lorebook.id == lorebook.id)
        .options(selectinload(Lorebook.entries))
    )
    lorebook = result.scalar_one()
    return LorebookResponse(
        id=lorebook.id,
        name=lorebook.name,
        description=lorebook.description,
        is_public=lorebook.is_public,
        is_active=lorebook.is_active,
        tag=lorebook.tag,
        entries=[_entry_to_response(e) for e in lorebook.entries],
    )

