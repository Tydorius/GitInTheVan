import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.audit import list_logs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])


class AuditLogItem(BaseModel):
    id: str
    action: str
    target_type: str
    target_id: str
    details: str
    ip_address: str
    created_at: str


class AuditLogListResponse(BaseModel):
    logs: list[AuditLogItem]
    total: int


@router.get("", response_model=AuditLogListResponse)
async def get_audit_logs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    logs = await list_logs(db, current_user.id, limit, offset)
    return AuditLogListResponse(
        logs=[AuditLogItem(**log) for log in logs],
        total=len(logs),
    )
