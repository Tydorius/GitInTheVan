import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.debug import clear_exchanges, get_exchange, list_exchanges

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])


class DebugExchangeListItem(BaseModel):
    id: str
    chat_id: str
    model: str
    created_at: str
    has_response: bool
    has_verification: bool
    stage_count: int


class DebugExchangeResponse(BaseModel):
    id: str
    chat_id: str
    model: str
    pipeline_data: dict
    response_content: str
    verification_data: dict
    created_at: str


class DebugListResponse(BaseModel):
    exchanges: list[DebugExchangeListItem]


@router.get("", response_model=DebugListResponse)
async def list_debug_exchanges(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    exchanges = await list_exchanges(current_user.id)
    items = [
        DebugExchangeListItem(
            id=e["id"],
            chat_id=e["chat_id"],
            model=e["model"],
            created_at=e["created_at"],
            has_response=bool(e.get("response_content")),
            has_verification=bool(e.get("verification_data")),
            stage_count=len(e.get("pipeline_data", {})),
        )
        for e in exchanges
    ]
    return DebugListResponse(exchanges=items)


@router.get("/{exchange_id}", response_model=DebugExchangeResponse)
async def get_debug_exchange(
    exchange_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    exchange = await get_exchange(current_user.id, exchange_id)
    if not exchange:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug exchange not found")
    return DebugExchangeResponse(**exchange)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_debug_exchanges(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await clear_exchanges(current_user.id)
