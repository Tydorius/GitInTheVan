"""Per-object version history: list, save, restore, delete (Phase 25).

The automatic half of this feature is not here -- `pre_edit` snapshots are taken
inside each resource router's own update and delete paths, so they cover the
user's edits in the UI and the assistant's edits through the same routes,
without either knowing this module exists. This router is the read side plus the
three deliberate actions: pin a version, restore it as a copy, restore it over
the live object.

Restore-as-new and restore-in-place are separate routes rather than one route
with a mode flag, because they carry different risk: the assistant registry
tiers the first WRITE and the second DESTRUCTIVE, and a mode argument would
collapse that distinction into something the permission model cannot see.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.resource_snapshot import ResourceSnapshot
from app.models.user import User
from app.services import snapshots as snapshot_service
from app.services.audit import log_action

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])


class SnapshotListItem(BaseModel):
    id: str
    resource_type: str
    resource_id: str
    resource_name: str
    content_hash: str
    source: str
    label: str
    size_bytes: int
    created_at: str


class SnapshotDetail(SnapshotListItem):
    content: dict


class SnapshotListResponse(BaseModel):
    snapshots: list[SnapshotListItem]


class SnapshotCreate(BaseModel):
    resource_type: str
    resource_id: str
    label: str = ""


class RestoreAsNewRequest(BaseModel):
    name: str = ""


class RestoreResponse(BaseModel):
    resource_type: str
    resource_id: str
    name: str
    created: bool
    notes: list[str]


def _to_item(row: ResourceSnapshot) -> SnapshotListItem:
    return SnapshotListItem(
        id=row.id,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        resource_name=row.resource_name,
        content_hash=row.content_hash,
        source=row.source,
        label=row.label,
        size_bytes=len(row.content_json or ""),
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


async def _load(db: AsyncSession, user_id: str, snapshot_id: str) -> ResourceSnapshot:
    result = await db.execute(
        select(ResourceSnapshot).where(
            ResourceSnapshot.id == snapshot_id, ResourceSnapshot.user_id == user_id
        )
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found")
    return snapshot


@router.get("", response_model=SnapshotListResponse)
async def list_snapshots(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    resource_type: Annotated[str, Query()] = "",
    resource_id: Annotated[str, Query()] = "",
):
    """Stored versions, newest first. Metadata only -- content comes from the detail route."""
    query = select(ResourceSnapshot).where(ResourceSnapshot.user_id == current_user.id)
    if resource_type:
        canonical = snapshot_service.resolve_type(resource_type)
        if not canonical:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Snapshots are not kept for '{resource_type}'",
            )
        query = query.where(ResourceSnapshot.resource_type == canonical)
    if resource_id:
        query = query.where(ResourceSnapshot.resource_id == resource_id)

    result = await db.execute(
        query.order_by(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc()).limit(200)
    )
    return SnapshotListResponse(snapshots=[_to_item(row) for row in result.scalars().all()])


@router.get("/{snapshot_id}", response_model=SnapshotDetail)
async def get_snapshot(
    snapshot_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """One stored version, with the content a restore would write."""
    snapshot = await _load(db, current_user.id, snapshot_id)
    try:
        content = snapshot_service.load_content(snapshot)
    except snapshot_service.SnapshotError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return SnapshotDetail(**_to_item(snapshot).model_dump(), content=content)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=SnapshotListItem)
async def create_snapshot(
    req: SnapshotCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Pin the object's current state. Manual snapshots are never pruned."""
    if not snapshot_service.resolve_type(req.resource_type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Snapshots are not kept for '{req.resource_type}'",
        )

    snapshot = await snapshot_service.capture(
        db,
        current_user.id,
        req.resource_type,
        req.resource_id,
        source=snapshot_service.SOURCE_MANUAL,
        label=req.label,
        strict=True,
    )
    if snapshot is None:
        # capture() returns None for a missing object and for one whose content
        # already matches the newest stored version.  Tell them apart, so "saved"
        # never means "silently did nothing".
        spec = snapshot_service.spec_for(req.resource_type)
        row = await snapshot_service.load_row(db, current_user.id, spec, req.resource_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Object not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This version is identical to the most recent snapshot already saved.",
        )

    await log_action(
        db, current_user.id, "snapshot_create", snapshot.resource_type, snapshot.resource_id,
        f"manual snapshot {snapshot.id}",
    )
    await db.commit()
    await db.refresh(snapshot)
    return _to_item(snapshot)


@router.post("/{snapshot_id}/restore-as-new", response_model=RestoreResponse)
async def restore_snapshot_as_new(
    snapshot_id: str,
    req: RestoreAsNewRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Recreate a stored version as a new object, leaving the original alone."""
    snapshot = await _load(db, current_user.id, snapshot_id)
    try:
        report = await snapshot_service.restore_as_new(
            db, current_user.id, snapshot, name=req.name
        )
    except snapshot_service.SnapshotError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await log_action(
        db, current_user.id, "snapshot_restore_as_new", report.resource_type, report.resource_id,
        f"from snapshot {snapshot.id}",
    )
    await db.commit()
    return RestoreResponse(**report.__dict__)


@router.post("/{snapshot_id}/restore-in-place", response_model=RestoreResponse)
async def restore_snapshot_in_place(
    snapshot_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Overwrite the live object with a stored version.

    A `pre_edit` snapshot of what is being overwritten is taken first, so this is
    itself undoable.
    """
    snapshot = await _load(db, current_user.id, snapshot_id)
    try:
        report = await snapshot_service.restore_in_place(db, current_user.id, snapshot)
    except snapshot_service.SnapshotError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await log_action(
        db, current_user.id, "snapshot_restore_in_place", report.resource_type,
        report.resource_id, f"from snapshot {snapshot.id}",
    )
    await db.commit()
    return RestoreResponse(**report.__dict__)


@router.delete("/{snapshot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_snapshot(
    snapshot_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Remove one stored version permanently."""
    snapshot = await _load(db, current_user.id, snapshot_id)
    await log_action(
        db, current_user.id, "snapshot_delete", snapshot.resource_type, snapshot.resource_id,
        f"snapshot {snapshot.id}",
    )
    await db.delete(snapshot)
    await db.commit()
