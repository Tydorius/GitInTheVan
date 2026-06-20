import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cantrip import Cantrip
from app.models.user import User
from app.services.cantrip import test_cantrip
from app.services.cantrip_context import build_context
from app.services.deno_runner import CantripExecutionError, CantripTimeoutError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cantrips", tags=["cantrips"])


class CantripCreate(BaseModel):
    name: str
    description: str = ""
    code: str = ""
    hook_type: str = "pre"
    is_public: bool = False
    is_active: bool = True
    execution_order: int = 10
    timeout_ms: int = 5000


class CantripUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    code: str | None = None
    hook_type: str | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    execution_order: int | None = None
    timeout_ms: int | None = None


class CantripResponse(BaseModel):
    id: str
    name: str
    description: str
    code: str
    hook_type: str
    is_public: bool
    is_active: bool
    execution_order: int
    timeout_ms: int


class CantripListItem(BaseModel):
    id: str
    name: str
    description: str
    hook_type: str
    is_public: bool
    is_active: bool
    execution_order: int
    timeout_ms: int


class CantripListResponse(BaseModel):
    cantrips: list[CantripListItem]


class TestMessageInput(BaseModel):
    role: str = "user"
    content: str = ""


class CantripTestRequest(BaseModel):
    code: str | None = None
    messages: list[TestMessageInput] | None = None
    conversation_id: str = "test-conversation"
    user_name: str = "User"
    character_name: str = ""
    character_description: str = ""
    first_message: str = ""
    alternate_greetings: list[str] = []
    chat_data: dict[str, Any] | None = None
    timeout_ms: int = 5000


class CantripTestResponse(BaseModel):
    personality: str
    scenario: str
    example_dialogs: str
    chat_data: dict[str, Any]
    debug_logs: list[str]
    error: str | None


def _cantrip_to_response(cantrip: Cantrip) -> CantripResponse:
    return CantripResponse(
        id=cantrip.id,
        name=cantrip.name,
        description=cantrip.description,
        code=cantrip.code,
        hook_type=cantrip.hook_type,
        is_public=cantrip.is_public,
        is_active=cantrip.is_active,
        execution_order=cantrip.execution_order,
        timeout_ms=cantrip.timeout_ms,
    )


def _cantrip_to_list_item(cantrip: Cantrip) -> CantripListItem:
    return CantripListItem(
        id=cantrip.id,
        name=cantrip.name,
        description=cantrip.description,
        hook_type=cantrip.hook_type,
        is_public=cantrip.is_public,
        is_active=cantrip.is_active,
        execution_order=cantrip.execution_order,
        timeout_ms=cantrip.timeout_ms,
    )


@router.get("")
async def list_cantrips(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Cantrip)
        .where(Cantrip.user_id == current_user.id)
        .order_by(Cantrip.execution_order, Cantrip.created_at)
    )
    cantrips = result.scalars().all()
    return CantripListResponse(cantrips=[_cantrip_to_list_item(s) for s in cantrips])


@router.get("/public")
async def list_public_cantrips(
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Cantrip)
        .where(Cantrip.is_public.is_(True), Cantrip.is_active.is_(True))
        .order_by(Cantrip.updated_at.desc())
    )
    cantrips = result.scalars().all()
    return CantripListResponse(cantrips=[_cantrip_to_list_item(s) for s in cantrips])


@router.get("/{cantrip_id}")
async def get_cantrip(
    cantrip_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Cantrip).where(Cantrip.id == cantrip_id))
    cantrip = result.scalar_one_or_none()
    if cantrip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")
    if cantrip.user_id != current_user.id and not cantrip.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")
    return _cantrip_to_response(cantrip)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_cantrip(
    req: CantripCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    cantrip = Cantrip(
        user_id=current_user.id,
        name=req.name,
        description=req.description,
        code=req.code,
        hook_type=req.hook_type,
        is_public=req.is_public,
        is_active=req.is_active,
        execution_order=req.execution_order,
        timeout_ms=req.timeout_ms,
    )
    db.add(cantrip)
    await db.commit()
    await db.refresh(cantrip)
    return _cantrip_to_response(cantrip)


@router.put("/{cantrip_id}")
async def update_cantrip(
    cantrip_id: str,
    req: CantripUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Cantrip).where(Cantrip.id == cantrip_id, Cantrip.user_id == current_user.id)
    )
    cantrip = result.scalar_one_or_none()
    if cantrip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")

    update_data = req.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(cantrip, key, value)

    await db.commit()
    await db.refresh(cantrip)
    return _cantrip_to_response(cantrip)


@router.delete("/{cantrip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cantrip(
    cantrip_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(Cantrip).where(Cantrip.id == cantrip_id, Cantrip.user_id == current_user.id)
    )
    cantrip = result.scalar_one_or_none()
    if cantrip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")
    await db.delete(cantrip)
    await db.commit()


@router.post("/test")
async def test_cantrip_endpoint(
    req: CantripTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    code = req.code
    if code is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code field is required for standalone test",
        )

    messages = (
        [{"role": m.role, "content": m.content} for m in req.messages]
        if req.messages
        else [{"role": "user", "content": "Hello"}]
    )

    context = build_context(
        messages=messages,
        conversation_id=req.conversation_id,
        user_name=req.user_name,
        character_name=req.character_name,
        character_description=req.character_description,
        first_message=req.first_message,
        alternate_greetings=req.alternate_greetings,
    )

    try:
        result = await test_cantrip(
            code=code,
            context=context,
            chat_data=req.chat_data,
            timeout_ms=req.timeout_ms,
        )
    except CantripTimeoutError:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"Cantrip timed out after {req.timeout_ms}ms",
        )
    except CantripExecutionError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

    return CantripTestResponse(
        personality=result.personality,
        scenario=result.scenario,
        example_dialogs=result.example_dialogs,
        chat_data=result.chat_data,
        debug_logs=result.debug_logs,
        error=result.error,
    )


@router.post("/{cantrip_id}/test")
async def test_stored_cantrip(
    cantrip_id: str,
    req: CantripTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result_query = await db.execute(select(Cantrip).where(Cantrip.id == cantrip_id))
    cantrip = result_query.scalar_one_or_none()
    if cantrip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cantrip not found")
    if cantrip.user_id != current_user.id and not cantrip.is_public:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    messages = (
        [{"role": m.role, "content": m.content} for m in req.messages]
        if req.messages
        else [{"role": "user", "content": "Hello"}]
    )

    context = build_context(
        messages=messages,
        conversation_id=req.conversation_id,
        user_name=req.user_name,
        character_name=req.character_name,
        character_description=req.character_description,
        first_message=req.first_message,
        alternate_greetings=req.alternate_greetings,
    )

    code = req.code if req.code is not None else cantrip.code

    try:
        test_result = await test_cantrip(
            code=code,
            context=context,
            chat_data=req.chat_data,
            timeout_ms=req.timeout_ms or cantrip.timeout_ms,
        )
    except CantripTimeoutError:
        raise HTTPException(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            detail=f"Cantrip timed out after {req.timeout_ms}ms",
        )
    except CantripExecutionError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )

    return CantripTestResponse(
        personality=test_result.personality,
        scenario=test_result.scenario,
        example_dialogs=test_result.example_dialogs,
        chat_data=test_result.chat_data,
        debug_logs=test_result.debug_logs,
        error=test_result.error,
    )
