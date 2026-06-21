import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.memory import Memory
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/memories", tags=["memories"])


class MemoryResponse(BaseModel):
    id: str
    conversation_id: str
    key: str
    value: str
    memory_type: str


class MemoryListResponse(BaseModel):
    memories: list[MemoryResponse]
    total: int


class MemoryUpdateRequest(BaseModel):
    value: str


@router.get("")
async def list_memories(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    conversation_id: str | None = None,
):
    query = select(Memory).where(Memory.user_id == current_user.id)
    if conversation_id:
        query = query.where(Memory.conversation_id == conversation_id)

    result = await db.execute(query.order_by(Memory.updated_at.desc()))
    memories = result.scalars().all()
    return MemoryListResponse(
        memories=[
            MemoryResponse(
                id=m.id,
                conversation_id=m.conversation_id,
                key=m.key,
                value=m.value,
                memory_type=m.memory_type,
            )
            for m in memories
        ],
        total=len(memories),
    )


@router.put("/{memory_id}")
async def update_memory(
    memory_id: str,
    req: MemoryUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == current_user.id)
    )
    memory = result.scalar_one_or_none()
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")

    memory.value = req.value
    await db.commit()
    await db.refresh(memory)
    return MemoryResponse(
        id=memory.id,
        conversation_id=memory.conversation_id,
        key=memory.key,
        value=memory.value,
        memory_type=memory.memory_type,
    )


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Memory).where(Memory.id == memory_id, Memory.user_id == current_user.id)
    )
    memory = result.scalar_one_or_none()
    if memory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory not found")
    await db.delete(memory)
    await db.commit()
