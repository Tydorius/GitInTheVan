import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cantrip import Cantrip
from app.models.user import User
from app.services.cantrip import test_cantrip
from app.services.cantrip_context import build_context
from app.services.deno_runner import CantripExecutionError, CantripTimeoutError
from app.templates.cantrip_templates import get_templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cantrips", tags=["cantrips"])


class CantripCreate(BaseModel):
    name: str
    description: str = ""
    llm_instructions: str = ""
    code: str = ""
    hook_type: str = "pre"
    run_pre_driver: bool = True
    run_driver_callable: bool = False
    run_pre_navigator: bool = False
    run_post_navigator: bool = False
    is_public: bool = False
    is_active: bool = True
    execution_order: int = 10
    timeout_ms: int = 5000
    tag: str = ""


class CantripUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    llm_instructions: str | None = None
    code: str | None = None
    hook_type: str | None = None
    run_pre_driver: bool | None = None
    run_driver_callable: bool | None = None
    run_pre_navigator: bool | None = None
    run_post_navigator: bool | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    execution_order: int | None = None
    timeout_ms: int | None = None
    tag: str | None = None


class CantripResponse(BaseModel):
    id: str
    name: str
    description: str
    llm_instructions: str
    code: str
    hook_type: str
    run_pre_driver: bool
    run_driver_callable: bool
    run_pre_navigator: bool
    run_post_navigator: bool
    is_public: bool
    is_active: bool
    execution_order: int
    timeout_ms: int
    tag: str


class CantripListItem(BaseModel):
    id: str
    name: str
    description: str
    llm_instructions: str
    code: str
    hook_type: str
    run_pre_driver: bool
    run_driver_callable: bool
    run_pre_navigator: bool
    run_post_navigator: bool
    is_public: bool
    is_active: bool
    execution_order: int
    timeout_ms: int
    tag: str


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
        llm_instructions=cantrip.llm_instructions,
        code=cantrip.code,
        hook_type=cantrip.hook_type,
        run_pre_driver=cantrip.run_pre_driver,
        run_driver_callable=cantrip.run_driver_callable,
        run_pre_navigator=cantrip.run_pre_navigator,
        run_post_navigator=cantrip.run_post_navigator,
        is_public=cantrip.is_public,
        is_active=cantrip.is_active,
        execution_order=cantrip.execution_order,
        timeout_ms=cantrip.timeout_ms,
        tag=cantrip.tag,
    )


def _cantrip_to_list_item(cantrip: Cantrip) -> CantripListItem:
    return CantripListItem(
        id=cantrip.id,
        name=cantrip.name,
        description=cantrip.description,
        llm_instructions=cantrip.llm_instructions,
        code=cantrip.code,
        hook_type=cantrip.hook_type,
        run_pre_driver=cantrip.run_pre_driver,
        run_driver_callable=cantrip.run_driver_callable,
        run_pre_navigator=cantrip.run_pre_navigator,
        run_post_navigator=cantrip.run_post_navigator,
        is_public=cantrip.is_public,
        is_active=cantrip.is_active,
        execution_order=cantrip.execution_order,
        timeout_ms=cantrip.timeout_ms,
        tag=cantrip.tag,
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


class TemplateInstall(BaseModel):
    template_name: str


@router.get("/templates")
async def list_templates(
    current_user: Annotated[User, Depends(get_current_user)],
):
    templates = get_templates()
    return {"templates": [{"name": t["name"], "description": t["description"]} for t in templates]}


@router.post("/templates/install", status_code=status.HTTP_201_CREATED)
async def install_template(
    req: TemplateInstall,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    templates = get_templates()
    template = next((t for t in templates if t["name"] == req.template_name), None)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template not found")

    cantrip = Cantrip(
        user_id=current_user.id,
        name=template["name"],
        description=template["description"],
        code=template["code"],
        hook_type=template.get("hook_type", "pre"),
        is_public=False,
        is_active=True,
        execution_order=template.get("execution_order", 10),
        timeout_ms=template.get("timeout_ms", 5000),
    )
    db.add(cantrip)
    await db.commit()
    await db.refresh(cantrip)
    return _cantrip_to_response(cantrip)


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
        llm_instructions=req.llm_instructions,
        code=req.code,
        hook_type=req.hook_type,
        run_pre_driver=req.run_pre_driver,
        run_driver_callable=req.run_driver_callable,
        run_pre_navigator=req.run_pre_navigator,
        run_post_navigator=req.run_post_navigator,
        is_public=req.is_public,
        is_active=req.is_active,
        execution_order=req.execution_order,
        timeout_ms=req.timeout_ms,
        tag=req.tag,
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

    if req.tag is not None and req.tag != cantrip.tag:
        existing = await db.execute(
            text("SELECT id FROM cantrips WHERE tag = :tag AND user_id = :uid AND id != :cid"),
            {"tag": req.tag, "uid": current_user.id, "cid": cantrip_id}
        )
        if existing.scalar_one_or_none() is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tag already in use")

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


class ValidateRequest(BaseModel):
    code: str


class ValidateResponse(BaseModel):
    valid: bool
    error: str | None = None


@router.post("/validate")
async def validate_cantrip(
    req: ValidateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    import asyncio
    import os
    import tempfile

    from app.services.deno_runner import DENO_PATH

    if not DENO_PATH:
        return ValidateResponse(valid=True, error=None)

    wrapper = f"""
const __userCode = {repr(req.code)};
try {{
    new Function('context', __userCode);
}} catch(e) {{
    Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify({{error: e.message}})));
    Deno.exit(1);
}}
Deno.stdout.writeSync(new TextEncoder().encode(JSON.stringify({{error: null}})));
"""

    fd, temp_path = tempfile.mkstemp(suffix=".mjs", prefix="gitv_validate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(wrapper)

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: __import__("subprocess").run(
                [DENO_PATH, "run", "--quiet", temp_path],
                capture_output=True, timeout=5,
            ),
        )

        stdout = result.stdout.decode("utf-8", errors="replace").strip() if result.stdout else ""
        if stdout:
            import json
            try:
                data = json.loads(stdout)
                if data.get("error"):
                    return ValidateResponse(valid=False, error=data["error"])
            except json.JSONDecodeError:
                pass

        return ValidateResponse(valid=True, error=None)
    except Exception as e:
        return ValidateResponse(valid=False, error=str(e))
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


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
