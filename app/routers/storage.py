"""Storage browser: CRUD over the cantrip persistent stores.

The three scopes (chat_data / cantrip_data / user_data) had no API before
this -- only the cantrip save/load helpers touched them. Every route is
scoped to the current user; a row id belonging to another user is a 404,
never a 403 leak oracle.

Writes are rule-18 content writes: the serialized value is size-checked
against the admin memory-rule cap (bounded below the MariaDB TEXT ceiling)
and run through ``sanitize_and_log``. Values must parse as JSON -- every
stored value is ``json.dumps`` output by construction, so an unparseable
edit is refused with 422 rather than corrupting a row the cantrip sandbox
would later fail to read.
"""

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cantrip import Cantrip
from app.models.cantrip_data import CantripData
from app.models.chat_data import ChatData
from app.models.user import User
from app.models.user_data import UserData
from app.services.admin import get_admin_settings
from app.services.content_guard import check_size, sanitize_and_log

router = APIRouter(prefix="/api/storage", tags=["storage"])

# MariaDB TEXT holds 65535 bytes; utf8mb4 worst-cases 4 bytes per character.
# The admin cap applies first, but it can be raised past what every dialect
# can store -- this is the hard ceiling that keeps a browser edit from
# failing at flush time on one backend and succeeding on another.
_VALUE_HARD_CEILING_CHARS = 60_000

_KEY_MAX = 256  # the column limit on all three tables
_PREVIEW_CHARS = 200
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    """Header-safe filename fragment: allow-list, never the raw input."""
    cleaned = _SLUG_RE.sub("-", name).strip("-.")[:64]
    return cleaned or "export"


def _parse_value_text(value_text: str, max_chars: int) -> str:
    """Validate a value edit: must be JSON, must fit the cap. Returns the
    canonical serialization to store."""
    try:
        parsed = json.loads(value_text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Value must be valid JSON: {exc}",
        ) from exc
    serialized = json.dumps(parsed, ensure_ascii=False)
    check_size(serialized, max_chars, "Storage value")
    return serialized


def _effective_value_cap(admin_settings: Any) -> int:
    return min(admin_settings.max_rule_size_kb * 1024, _VALUE_HARD_CEILING_CHARS)


def _validate_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Key must not be empty",
        )
    if len(key) > _KEY_MAX:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Key exceeds {_KEY_MAX} characters",
        )
    return key


def _preview(serialized: str) -> str:
    return serialized[:_PREVIEW_CHARS] + ("…" if len(serialized) > _PREVIEW_CHARS else "")


def _row_out(row: Any, full: bool = False) -> dict[str, Any]:
    try:
        value = json.loads(row.value_json) if row.value_json else None
    except (json.JSONDecodeError, ValueError):
        value = row.value_json
    out: dict[str, Any] = {
        "id": row.id,
        "key": row.key,
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }
    if full:
        out["value"] = value
    else:
        out["preview"] = _preview(row.value_json or "")
        out["size"] = len(row.value_json or "")
    return out


async def _owned_row(db: AsyncSession, model: Any, row_id: str, user_id: str) -> Any:
    row = (await db.execute(
        select(model).where(model.id == row_id, model.user_id == user_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Row not found")
    return row


# ---------------------------------------------------------------------------
# Chat data


class StorageConversation(BaseModel):
    conversation_id: str
    key_count: int
    updated_at: str


class ConversationListResponse(BaseModel):
    conversations: list[StorageConversation]
    total: int


@router.get("/chats", response_model=ConversationListResponse)
async def list_conversations(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: str = "",
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    base = (
        select(
            ChatData.conversation_id,
            func.count(ChatData.id).label("key_count"),
            func.max(ChatData.updated_at).label("updated_at"),
        )
        .where(ChatData.user_id == current_user.id)
        .group_by(ChatData.conversation_id)
    )
    if search:
        base = base.where(ChatData.conversation_id.contains(search))
    subq = base.subquery()
    total = (await db.execute(select(func.count()).select_from(subq))).scalar() or 0
    rows = (await db.execute(base.order_by(func.max(ChatData.updated_at).desc()).limit(limit).offset(offset))).all()
    return ConversationListResponse(
        conversations=[
            StorageConversation(
                conversation_id=r.conversation_id,
                key_count=int(r.key_count),
                updated_at=r.updated_at.isoformat() if r.updated_at else "",
            )
            for r in rows
        ],
        total=int(total),
    )


class RowListResponse(BaseModel):
    rows: list[dict[str, Any]]
    total: int


@router.get("/chat-data", response_model=RowListResponse)
async def list_chat_data(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    conversation_id: str = Query(min_length=1),
    search: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    filters = [
        ChatData.user_id == current_user.id,
        ChatData.conversation_id == conversation_id,
    ]
    if search:
        filters.append(ChatData.key.contains(search))
    total = (await db.execute(
        select(func.count()).select_from(ChatData).where(*filters)
    )).scalar() or 0
    rows = (await db.execute(
        select(ChatData).where(*filters).order_by(ChatData.key).limit(limit).offset(offset)
    )).scalars().all()
    return RowListResponse(rows=[_row_out(r) for r in rows], total=int(total))


@router.get("/chat-data/{row_id}")
async def get_chat_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, ChatData, row_id, current_user.id)
    return _row_out(row, full=True)


class StoragePutRequest(BaseModel):
    key: str | None = None
    value: str  # raw JSON text; validated server-side


@router.put("/chat-data/{row_id}")
async def put_chat_data_row(
    row_id: str,
    req: StoragePutRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, ChatData, row_id, current_user.id)
    admin_settings = await get_admin_settings()
    serialized = _parse_value_text(req.value, _effective_value_cap(admin_settings))
    if req.key is not None:
        new_key = _validate_key(req.key)
        clash = (await db.execute(
            select(ChatData.id).where(
                ChatData.user_id == current_user.id,
                ChatData.conversation_id == row.conversation_id,
                ChatData.key == new_key,
                ChatData.id != row_id,
            )
        )).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Key '{new_key}' already exists in this conversation",
            )
        row.key = new_key
    row.value_json = await sanitize_and_log(
        db, current_user.id, serialized, "chat_data", row_id
    )
    row.updated_at = datetime.now(UTC)
    await db.commit()
    return _row_out(row, full=True)


@router.delete("/chat-data/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, ChatData, row_id, current_user.id)
    await db.delete(row)
    await db.commit()


class CopyRowRequest(BaseModel):
    target_conversation_id: str = Field(min_length=1, max_length=256)


@router.post("/chat-data/{row_id}/copy")
async def copy_chat_data_row(
    row_id: str,
    req: CopyRowRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, ChatData, row_id, current_user.id)
    existing = (await db.execute(
        select(ChatData).where(
            ChatData.user_id == current_user.id,
            ChatData.conversation_id == req.target_conversation_id,
            ChatData.key == row.key,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Key '{row.key}' already exists in the target conversation",
        )
    new_row = ChatData(
        user_id=current_user.id,
        conversation_id=req.target_conversation_id,
        key=row.key,
        value_json=row.value_json,
    )
    db.add(new_row)
    await db.commit()
    return _row_out(new_row, full=True)


@router.delete("/chats/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await db.execute(
        delete(ChatData).where(
            ChatData.user_id == current_user.id,
            ChatData.conversation_id == conversation_id,
        )
    )
    await db.commit()


class CopyConversationRequest(BaseModel):
    target_conversation_id: str = Field(min_length=1, max_length=256)


class CopyConversationResponse(BaseModel):
    copied: int
    skipped: list[str]


@router.post("/chats/{conversation_id}/copy", response_model=CopyConversationResponse)
async def copy_conversation(
    conversation_id: str,
    req: CopyConversationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(ChatData).where(
            ChatData.user_id == current_user.id,
            ChatData.conversation_id == conversation_id,
        )
    )).scalars().all()
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")

    existing_keys = set((await db.execute(
        select(ChatData.key).where(
            ChatData.user_id == current_user.id,
            ChatData.conversation_id == req.target_conversation_id,
        )
    )).scalars().all())

    copied = 0
    skipped: list[str] = []
    for row in rows:
        if row.key in existing_keys:
            skipped.append(row.key)
            continue
        db.add(ChatData(
            user_id=current_user.id,
            conversation_id=req.target_conversation_id,
            key=row.key,
            value_json=row.value_json,
        ))
        existing_keys.add(row.key)
        copied += 1
    await db.commit()
    return CopyConversationResponse(copied=copied, skipped=skipped)


# ---------------------------------------------------------------------------
# User data


@router.get("/user-data", response_model=RowListResponse)
async def list_user_data(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    search: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    filters = [UserData.user_id == current_user.id]
    if search:
        filters.append(UserData.key.contains(search))
    total = (await db.execute(
        select(func.count()).select_from(UserData).where(*filters)
    )).scalar() or 0
    rows = (await db.execute(
        select(UserData).where(*filters).order_by(UserData.key).limit(limit).offset(offset)
    )).scalars().all()
    return RowListResponse(rows=[_row_out(r) for r in rows], total=int(total))


@router.get("/user-data/{row_id}")
async def get_user_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, UserData, row_id, current_user.id)
    return _row_out(row, full=True)


@router.put("/user-data/{row_id}")
async def put_user_data_row(
    row_id: str,
    req: StoragePutRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, UserData, row_id, current_user.id)
    admin_settings = await get_admin_settings()
    serialized = _parse_value_text(req.value, _effective_value_cap(admin_settings))
    if req.key is not None:
        new_key = _validate_key(req.key)
        clash = (await db.execute(
            select(UserData.id).where(
                UserData.user_id == current_user.id,
                UserData.key == new_key,
                UserData.id != row_id,
            )
        )).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Key '{new_key}' already exists",
            )
        row.key = new_key
    row.value_json = await sanitize_and_log(
        db, current_user.id, serialized, "user_data", row_id
    )
    row.updated_at = datetime.now(UTC)
    await db.commit()
    return _row_out(row, full=True)


@router.delete("/user-data/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, UserData, row_id, current_user.id)
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------------------
# Cantrip data


@router.get("/cantrip-data", response_model=RowListResponse)
async def list_cantrip_data(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cantrip_id: str = Query(min_length=1),
    search: str = "",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    filters = [
        CantripData.user_id == current_user.id,
        CantripData.cantrip_id == cantrip_id,
    ]
    if search:
        filters.append(CantripData.key.contains(search))
    total = (await db.execute(
        select(func.count()).select_from(CantripData).where(*filters)
    )).scalar() or 0
    rows = (await db.execute(
        select(CantripData).where(*filters).order_by(CantripData.key).limit(limit).offset(offset)
    )).scalars().all()
    return RowListResponse(rows=[_row_out(r) for r in rows], total=int(total))


@router.get("/cantrip-data/{row_id}")
async def get_cantrip_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, CantripData, row_id, current_user.id)
    return _row_out(row, full=True)


@router.put("/cantrip-data/{row_id}")
async def put_cantrip_data_row(
    row_id: str,
    req: StoragePutRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, CantripData, row_id, current_user.id)
    admin_settings = await get_admin_settings()
    serialized = _parse_value_text(req.value, _effective_value_cap(admin_settings))
    if req.key is not None:
        new_key = _validate_key(req.key)
        clash = (await db.execute(
            select(CantripData.id).where(
                CantripData.user_id == current_user.id,
                CantripData.cantrip_id == row.cantrip_id,
                CantripData.key == new_key,
                CantripData.id != row_id,
            )
        )).scalar_one_or_none()
        if clash is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Key '{new_key}' already exists for this cantrip",
            )
        row.key = new_key
    row.value_json = await sanitize_and_log(
        db, current_user.id, serialized, "cantrip_data", row_id
    )
    row.updated_at = datetime.now(UTC)
    await db.commit()
    return _row_out(row, full=True)


@router.delete("/cantrip-data/{row_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cantrip_data_row(
    row_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _owned_row(db, CantripData, row_id, current_user.id)
    await db.delete(row)
    await db.commit()


# ---------------------------------------------------------------------------
# Export / import


def _decode_row_value(value_json: str | None) -> Any:
    if not value_json:
        return None
    try:
        return json.loads(value_json)
    except (json.JSONDecodeError, ValueError):
        return value_json


def _export_payload(
    scope: str,
    rows: list[Any],
    context_id: str = "",
    cantrip_name: str = "",
) -> dict[str, Any]:
    return {
        "format": "gitv-storage-export",
        "version": 1,
        "scope": scope,
        "conversation_id": context_id,
        "cantrip_name": cantrip_name,
        "exported_at": datetime.now(UTC).isoformat(),
        "rows": [
            {"key": r.key, "value": _decode_row_value(r.value_json)}
            for r in rows
        ],
    }


@router.get("/export")
async def export_storage(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    scope: str = Query(pattern="^(chats|chat|user|cantrip|all)$"),
    conversation_id: str = "",
    cantrip_id: str = "",
):
    from fastapi.responses import Response

    name_parts = ["gitv", "storage", scope]
    payloads: list[dict[str, Any]] = []

    if scope in ("chats", "all"):
        conversations = (await db.execute(
            select(ChatData.conversation_id).where(
                ChatData.user_id == current_user.id
            ).distinct()
        )).scalars().all()
        for conv in conversations:
            rows = (await db.execute(
                select(ChatData).where(
                    ChatData.user_id == current_user.id,
                    ChatData.conversation_id == conv,
                ).order_by(ChatData.key)
            )).scalars().all()
            payloads.append(_export_payload("chat", rows, context_id=conv))

    if scope == "chat" and conversation_id:
        rows = (await db.execute(
            select(ChatData).where(
                ChatData.user_id == current_user.id,
                ChatData.conversation_id == conversation_id,
            ).order_by(ChatData.key)
        )).scalars().all()
        payloads.append(_export_payload("chat", rows, context_id=conversation_id))
        name_parts.append(_slug(conversation_id))

    if scope in ("user", "all"):
        rows = (await db.execute(
            select(UserData).where(UserData.user_id == current_user.id).order_by(UserData.key)
        )).scalars().all()
        payloads.append(_export_payload("user", rows))

    if scope in ("cantrip", "all") and cantrip_id:
        cantrip = (await db.execute(
            select(Cantrip).where(
                Cantrip.id == cantrip_id, Cantrip.user_id == current_user.id
            )
        )).scalar_one_or_none()
        if cantrip is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")
        rows = (await db.execute(
            select(CantripData).where(
                CantripData.user_id == current_user.id,
                CantripData.cantrip_id == cantrip_id,
            ).order_by(CantripData.key)
        )).scalars().all()
        payloads.append(_export_payload("cantrip", rows, cantrip_name=cantrip.name))
        name_parts.append(_slug(cantrip.name))
    elif scope == "all":
        cantrip_ids = (await db.execute(
            select(CantripData.cantrip_id).where(
                CantripData.user_id == current_user.id
            ).distinct()
        )).scalars().all()
        for cid in cantrip_ids:
            cantrip = (await db.execute(
                select(Cantrip).where(
                    Cantrip.id == cid, Cantrip.user_id == current_user.id
                )
            )).scalar_one_or_none()
            if cantrip is None:
                continue
            rows = (await db.execute(
                select(CantripData).where(
                    CantripData.user_id == current_user.id,
                    CantripData.cantrip_id == cid,
                ).order_by(CantripData.key)
            )).scalars().all()
            payloads.append(_export_payload("cantrip", rows, cantrip_name=cantrip.name))

    if not payloads:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Nothing to export for the given scope and filters",
        )

    body = payloads[0] if len(payloads) == 1 else {"format": "gitv-storage-export", "version": 1, "bundles": payloads}
    return Response(
        content=json.dumps(body, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{_slug("-".join(name_parts))}.json"'},
    )


class ImportRowInput(BaseModel):
    key: str = Field(min_length=1, max_length=_KEY_MAX)
    value: Any = None


class ImportRequest(BaseModel):
    # Accepts a single export bundle or the multi-bundle wrapper.
    payload: dict[str, Any]
    # For chat-scope imports when the bundle itself carries no conversation:
    target_conversation_id: str = ""
    target_cantrip_id: str = ""


class ImportResponse(BaseModel):
    imported: int
    skipped: list[str]


@router.post("/import", response_model=ImportResponse)
async def import_storage(
    req: ImportRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    admin_settings = await get_admin_settings()
    cap = _effective_value_cap(admin_settings)

    bundles: list[dict[str, Any]] = []
    payload = req.payload or {}
    if payload.get("format") == "gitv-storage-export" and isinstance(payload.get("bundles"), list):
        bundles = [b for b in payload["bundles"] if isinstance(b, dict)]
    elif payload.get("format") == "gitv-storage-export":
        bundles = [payload]
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Not a GitV storage export payload",
        )

    imported = 0
    skipped: list[str] = []

    for bundle in bundles:
        scope = bundle.get("scope", "")
        rows_in = bundle.get("rows", [])
        if not isinstance(rows_in, list):
            continue

        conversation_id = (
            req.target_conversation_id or bundle.get("conversation_id", "")
        )
        cantrip_id = req.target_cantrip_id

        if scope == "chat":
            if not conversation_id:
                skipped.append("(chat bundle without a conversation id)")
                continue
            model, extra = ChatData, {"conversation_id": conversation_id}
        elif scope == "user":
            model, extra = UserData, {}
        elif scope == "cantrip":
            if not cantrip_id:
                skipped.append("(cantrip bundle without a target cantrip)")
                continue
            owned_cantrip = (await db.execute(
                select(Cantrip.id).where(
                    Cantrip.id == cantrip_id,
                    Cantrip.user_id == current_user.id,
                )
            )).scalar_one_or_none()
            if owned_cantrip is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")
            model, extra = CantripData, {"cantrip_id": cantrip_id}
        else:
            skipped.append(f"(unknown scope '{scope}')")
            continue

        for row_in in rows_in:
            if not isinstance(row_in, dict):
                continue
            key = str(row_in.get("key", "")).strip()
            if not key or len(key) > _KEY_MAX:
                skipped.append(f"{key or '(empty key)'} (invalid key)")
                continue
            serialized = json.dumps(row_in.get("value"), ensure_ascii=False)
            try:
                check_size(serialized, cap, "Storage value")
                sanitized = await sanitize_and_log(
                    db, current_user.id, serialized, f"storage_import_{scope}", key
                )
            except HTTPException as exc:
                skipped.append(f"{key} ({exc.detail})")
                continue

            existing = (await db.execute(
                select(model).where(
                    model.user_id == current_user.id,
                    model.key == key,
                    *(
                        [model.conversation_id == conversation_id]
                        if scope == "chat" else
                        [model.cantrip_id == cantrip_id] if scope == "cantrip" else []
                    ),
                )
            )).scalar_one_or_none()
            if existing is not None:
                existing.value_json = sanitized
                existing.updated_at = datetime.now(UTC)
            else:
                db.add(model(
                    user_id=current_user.id,
                    key=key,
                    value_json=sanitized,
                    **extra,
                ))
            imported += 1

    await db.commit()
    return ImportResponse(imported=imported, skipped=skipped)
