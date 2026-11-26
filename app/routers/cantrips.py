import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cantrip import Cantrip
from app.models.user import User
from app.routers._helpers import check_tag_unique, get_owned_or_404
from app.services import snapshots
from app.services.admin import get_admin_settings
from app.services.budget import estimate_messages_tokens, estimate_tokens
from app.services.cantrip import _load_user_data, select_user_data_seed, test_cantrip
from app.services.cantrip_context import build_context
from app.services.content_guard import check_size, gate_scan_findings, log_scan_findings
from app.services.deno_runner import CantripExecutionError, CantripResult, CantripTimeoutError
from app.services.dynamic_lorebook import parse_reads
from app.services.response_tags import parse_tag_names
from app.services.safety_scanner import MAX_CONTENT_SIZE, scan_cantrip
from app.templates.cantrip_templates import get_templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cantrips", tags=["cantrips"])


async def _test_with_passenger(
    code: str,
    context: dict,
    chat_data: dict | None,
    timeout_ms: int,
    user_id: str,
    user_data: dict | None = None,
) -> CantripResult:
    """Run a test cantrip with Passenger available, as the pipeline would run
    it: the caller's own gates, endpoints and budgets, against a synthetic
    body marked sandbox -- Passenger calls are recorded but excluded from the
    default aggregates, because a Tester run is not live traffic."""
    from app.services.metrics import flush_metrics, init_metrics
    from app.services.passenger import get_passenger_handler

    body_json: dict[str, Any] = {"_gitv_sandbox": True, "messages": []}
    init_metrics(body_json, user_id)
    handler = await get_passenger_handler(body_json, user_id)
    try:
        return await test_cantrip(
            code=code, context=context, chat_data=chat_data,
            user_data=user_data,
            timeout_ms=timeout_ms, passenger_handler=handler,
        )
    finally:
        await flush_metrics(body_json)


async def _check_and_scan_cantrip_code(
    user_id: str, code: str, cantrip_id: str = "",
    override: bool = False,
) -> None:
    """Cantrip code is executable JavaScript — unlike text content it's never run
    through sanitize_input()'s control-char stripping (could corrupt valid source),
    only size-checked and pattern-scanned. Critical findings block the save
    unless the request explicitly overrides them. See
    Planning/security-control-document.md.
    """
    admin_settings = await get_admin_settings()
    max_chars = admin_settings.max_script_size_kb * 1024
    check_size(code, max_chars, "Cantrip code")
    scan = scan_cantrip(code, max_content_size=max(MAX_CONTENT_SIZE, max_chars))
    await log_scan_findings(user_id, scan, "cantrip", cantrip_id)
    gate_scan_findings(scan, override, "Cantrip code")


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
    budget_weight: float = 1.0
    reserved_tokens: int = Field(default=0, ge=0)
    # Phase 20: lorebook names whose entries are seeded read-only into the
    # sandbox (context.lorebook reads).
    lorebook_reads: list[str] = []
    # Package 3 (item 3): tag names this cantrip registers for extraction
    # from the model's final reply. Normalized lowercase; per-user unique.
    response_tags: list[str] = []
    # 0.45.0: user_data key prefixes this cantrip is allowed to access.
    user_data_prefixes: list[str] = []
    # 0.50.0 (N13): keep card-field appends in messages[0] under cache_friendly_mode.
    card_edits_standing: bool = False
    # Set by a client that has already shown the scanner findings to the user
    # and been told to proceed anyway. Not a model column.
    override_scan: bool = False


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
    budget_weight: float | None = None
    reserved_tokens: int | None = Field(default=None, ge=0)
    lorebook_reads: list[str] | None = None
    response_tags: list[str] | None = None
    user_data_prefixes: list[str] | None = None
    card_edits_standing: bool | None = None
    override_scan: bool = False


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
    budget_weight: float
    reserved_tokens: int = 0
    lorebook_reads: list[str]
    response_tags: list[str] = []
    user_data_prefixes: list[str] = []
    card_edits_standing: bool = False


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
    budget_weight: float
    reserved_tokens: int = 0
    lorebook_reads: list[str]
    response_tags: list[str] = []
    user_data_prefixes: list[str] = []
    card_edits_standing: bool = False


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
    user_data: dict[str, Any] | None = None
    user_data_prefixes: list[str] | None = None
    timeout_ms: int = 5000


class CantripTestResponse(BaseModel):
    personality: str
    scenario: str
    example_dialogs: str
    chat_data: dict[str, Any]
    user_data: dict[str, Any] = Field(default_factory=dict)
    debug_logs: list[str]
    error: str | None
    tokens_before: int = 0
    tokens_after: int = 0
    token_delta: int = 0


class SequenceTurnInput(BaseModel):
    """One turn of a sequence-tester script: the user message the client
    would send and the canned model reply standing in for the forward.
    ``messages_override`` replaces the accumulated history for this turn
    (trimming / swipe scenarios) and becomes the new baseline for later
    turns."""

    user_message: str = Field(default="", max_length=65536)
    reply: str = Field(default="", max_length=65536)
    messages_override: list[TestMessageInput] | None = None
    is_regenerate: bool = False


class SequenceTestRequest(BaseModel):
    conversation_id: str = ""
    user_name: str = "User"
    model: str = ""
    initial_messages: list[TestMessageInput] | None = None
    seed_chat_data: dict[str, Any] | None = None
    seed_user_data: dict[str, Any] | None = None
    seed_cantrip_data: dict[str, dict[str, Any]] | None = None
    start_turn_index: int = 0
    only_cantrip_ids: list[str] | None = None
    turns: list[SequenceTurnInput] = Field(max_length=50)


class SequenceTestResponse(BaseModel):
    conversation_id: str
    turns: list[dict[str, Any]]
    stopped_at_turn: int | None
    stop_reason: str
    final_chat_data: dict[str, Any]
    final_user_data: dict[str, Any]
    final_cantrip_data: dict[str, dict[str, Any]]
    total_token_delta: int = 0


def _normalize_string_list(items: list[str] | None) -> list[str]:
    """Strip, deduplicate, and cap a config string list (max 20 items, 128 chars each)."""
    out: list[str] = []
    for item in items or []:
        cleaned = str(item).strip()[:128]
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:20]


def _normalize_reads(names: list[str] | None) -> list[str]:
    """lorebook_reads as stored: stripped, de-duplicated, capped at 20 names
    of at most 128 chars each (a config list, not content — the cap keeps the
    column bounded, the names themselves name objects the user owns)."""
    return _normalize_string_list(names)


def _parse_prefixes(raw: str | None) -> list[str]:
    """Parse stored JSON user_data_prefixes tolerantly (NULL on MySQL -> [])."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return _normalize_string_list([str(p) for p in parsed])


def _compute_test_token_metrics(
    context: dict[str, Any],
    messages: list[dict[str, Any]],
    result: CantripResult,
) -> tuple[int, int, int]:
    """Return (tokens_before, tokens_after, token_delta) for a single-run test."""
    char = context.get("character") or {}
    msg_tokens = estimate_messages_tokens(messages)
    before_fields = (
        str(char.get("personality", "") or ""),
        str(char.get("scenario", "") or ""),
        str(char.get("example_dialogs", "") or ""),
    )
    tokens_before = msg_tokens + sum(estimate_tokens(t) for t in before_fields if t)

    after_fields = (
        str(result.personality or ""),
        str(result.scenario or ""),
        str(result.example_dialogs or ""),
    )
    injections = getattr(result, "injections", None) or []
    inject_tokens = sum(
        estimate_tokens(str(inj.get("text", "")))
        for inj in injections
        if isinstance(inj, dict) and inj.get("text")
    )
    tokens_after = msg_tokens + sum(estimate_tokens(t) for t in after_fields if t) + inject_tokens
    return tokens_before, tokens_after, tokens_after - tokens_before


async def _validate_response_tags(
    db: AsyncSession, user_id: str, names: list[str], exclude_cantrip_id: str = ""
) -> str:
    """Normalize + validate a response_tags list, returning the stored JSON.

    Refuses (422) names that are not legal ([a-z0-9_-]{1,32}, lowercase) or
    are reserved, and names another of the user's cantrips already
    registered -- per-user uniqueness keeps tag ownership unambiguous, like
    the activation-tag column.
    """
    from app.services.response_tags import normalize_tag_name

    cleaned: list[str] = []
    for raw in names[:20]:
        name = normalize_tag_name(raw)
        if name is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{raw}' is not a valid response tag name "
                       "(lowercase letters, digits, - or _, max 32 chars, not reserved)",
            )
        if name not in cleaned:
            cleaned.append(name)

    rows = (
        await db.execute(select(Cantrip.id, Cantrip.response_tags).where(Cantrip.user_id == user_id))
    ).all()
    for row_id, raw_tags in rows:
        if row_id == exclude_cantrip_id:
            continue
        for taken in parse_tag_names(raw_tags):
            if taken in cleaned:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Response tag '{taken}' is already registered by another cantrip",
                )
    return json.dumps(cleaned)


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
        budget_weight=cantrip.budget_weight,
        reserved_tokens=getattr(cantrip, "reserved_tokens", 0) or 0,
        lorebook_reads=parse_reads(getattr(cantrip, "lorebook_reads", None)),
        response_tags=parse_tag_names(getattr(cantrip, "response_tags", None)),
        user_data_prefixes=_parse_prefixes(getattr(cantrip, "user_data_prefixes", None)),
        card_edits_standing=bool(getattr(cantrip, "card_edits_standing", False)),
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
        budget_weight=cantrip.budget_weight,
        reserved_tokens=getattr(cantrip, "reserved_tokens", 0) or 0,
        lorebook_reads=parse_reads(getattr(cantrip, "lorebook_reads", None)),
        response_tags=parse_tag_names(getattr(cantrip, "response_tags", None)),
        user_data_prefixes=_parse_prefixes(getattr(cantrip, "user_data_prefixes", None)),
        card_edits_standing=bool(getattr(cantrip, "card_edits_standing", False)),
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
        # A template whose defaults would actively rewrite prompts (the
        # Prompt Janitor) ships disabled; everything else installs active.
        is_active=template.get("is_active", True),
        execution_order=template.get("execution_order", 10),
        timeout_ms=template.get("timeout_ms", 5000),
        # Optional position flags so a template can ship response-side
        # (pre/post-Navigator); default is the historical pre-driver-only.
        run_pre_driver=template.get("run_pre_driver", template.get("hook_type", "pre") == "pre"),
        run_driver_callable=template.get("run_driver_callable", False),
        run_pre_navigator=template.get("run_pre_navigator", False),
        run_post_navigator=template.get("run_post_navigator", False),
        card_edits_standing=template.get("card_edits_standing", False),
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
    await _check_and_scan_cantrip_code(current_user.id, req.code, override=req.override_scan)

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
        budget_weight=req.budget_weight,
        reserved_tokens=req.reserved_tokens,
        lorebook_reads=json.dumps(_normalize_reads(req.lorebook_reads)),
        response_tags=await _validate_response_tags(db, current_user.id, req.response_tags),
        user_data_prefixes=json.dumps(_normalize_string_list(req.user_data_prefixes)),
        card_edits_standing=req.card_edits_standing,
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
    cantrip = await get_owned_or_404(db, Cantrip, cantrip_id, current_user.id, "Cantrip")

    if req.tag and req.tag != cantrip.tag:
        await check_tag_unique(db, Cantrip, current_user.id, req.tag, exclude_id=cantrip_id)

    update_data = req.model_dump(exclude_unset=True)
    update_data.pop("override_scan", None)
    if "lorebook_reads" in update_data and update_data["lorebook_reads"] is not None:
        # The column is JSON text; the API speaks lists.
        update_data["lorebook_reads"] = json.dumps(_normalize_reads(update_data["lorebook_reads"]))
    if "response_tags" in update_data and update_data["response_tags"] is not None:
        update_data["response_tags"] = await _validate_response_tags(
            db, current_user.id, update_data["response_tags"],
            exclude_cantrip_id=cantrip_id,
        )
    if "user_data_prefixes" in update_data and update_data["user_data_prefixes"] is not None:
        update_data["user_data_prefixes"] = json.dumps(
            _normalize_string_list(update_data["user_data_prefixes"])
        )
    if "code" in update_data and update_data["code"] is not None:
        await _check_and_scan_cantrip_code(
            current_user.id, update_data["code"], cantrip_id, override=req.override_scan
        )

    await snapshots.capture(db, current_user.id, "cantrip", cantrip_id)

    for key, value in update_data.items():
        setattr(cantrip, key, value)

    await db.commit()
    await db.refresh(cantrip)
    return _cantrip_to_response(cantrip)


def _copy_name(name: str, max_len: int = 128) -> str:
    suffix = " (Copy)"
    limit = max_len - len(suffix)
    return f"{name[:limit]}{suffix}" if len(name) > limit else f"{name}{suffix}"


@router.delete("/{cantrip_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cantrip(
    cantrip_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    cantrip = await get_owned_or_404(db, Cantrip, cantrip_id, current_user.id, "Cantrip")

    await snapshots.capture(db, current_user.id, "cantrip", cantrip_id)
    await db.delete(cantrip)
    await db.commit()


@router.post("/{cantrip_id}/duplicate", response_model=CantripResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_cantrip(
    cantrip_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    source = await get_owned_or_404(db, Cantrip, cantrip_id, current_user.id, "Cantrip")

    copy = Cantrip(
        user_id=current_user.id,
        name=_copy_name(source.name, 128),
        description=source.description,
        llm_instructions=source.llm_instructions,
        code=source.code,
        hook_type=source.hook_type,
        run_pre_driver=source.run_pre_driver,
        run_driver_callable=source.run_driver_callable,
        run_pre_navigator=source.run_pre_navigator,
        run_post_navigator=source.run_post_navigator,
        is_public=False,
        is_active=False,
        tag=source.tag,
        execution_order=source.execution_order,
        timeout_ms=source.timeout_ms,
        budget_weight=source.budget_weight,
        reserved_tokens=getattr(source, "reserved_tokens", 0) or 0,
        lorebook_reads=source.lorebook_reads if source.lorebook_reads is not None else "[]",
        response_tags=source.response_tags if source.response_tags is not None else "[]",
        user_data_prefixes=source.user_data_prefixes if source.user_data_prefixes is not None else "[]",
        card_edits_standing=bool(getattr(source, "card_edits_standing", False)),
    )
    db.add(copy)
    await db.commit()
    await db.refresh(copy)
    return _cantrip_to_response(copy)


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

    from app.services.deno_runner import validate_cantrip_syntax

    loop = asyncio.get_event_loop()
    error = await loop.run_in_executor(None, validate_cantrip_syntax, req.code)
    if error:
        return ValidateResponse(valid=False, error=error)
    return ValidateResponse(valid=True, error=None)


@router.post("/test")
async def test_cantrip_endpoint(
    req: CantripTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
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

    raw_user_data = (
        req.user_data
        if req.user_data is not None
        else await _load_user_data(db, current_user.id)
    )
    seeded_user_data, _ = select_user_data_seed(
        raw_user_data, code, req.user_data_prefixes
    )

    try:
        result = await _test_with_passenger(
            code=code,
            context=context,
            chat_data=req.chat_data,
            timeout_ms=req.timeout_ms,
            user_id=current_user.id,
            user_data=seeded_user_data,
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

    tokens_before, tokens_after, token_delta = _compute_test_token_metrics(
        context, messages, result
    )
    return CantripTestResponse(
        personality=result.personality,
        scenario=result.scenario,
        example_dialogs=result.example_dialogs,
        chat_data=result.chat_data,
        user_data=result.user_data,
        debug_logs=result.debug_logs,
        error=result.error,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        token_delta=token_delta,
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
    raw_user_data = (
        req.user_data
        if req.user_data is not None
        else await _load_user_data(db, current_user.id)
    )
    prefixes = (
        req.user_data_prefixes
        if req.user_data_prefixes is not None
        else cantrip.user_data_prefixes
    )
    seeded_user_data, _ = select_user_data_seed(raw_user_data, code, prefixes)

    try:
        test_result = await _test_with_passenger(
            code=code,
            context=context,
            chat_data=req.chat_data,
            timeout_ms=req.timeout_ms or cantrip.timeout_ms,
            user_id=current_user.id,
            user_data=seeded_user_data,
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

    tokens_before, tokens_after, token_delta = _compute_test_token_metrics(
        context, messages, test_result
    )
    return CantripTestResponse(
        personality=test_result.personality,
        scenario=test_result.scenario,
        example_dialogs=test_result.example_dialogs,
        chat_data=test_result.chat_data,
        user_data=test_result.user_data,
        debug_logs=test_result.debug_logs,
        error=test_result.error,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        token_delta=token_delta,
    )


@router.post("/test-sequence", response_model=SequenceTestResponse)
async def test_sequence_endpoint(
    req: SequenceTestRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    """Multi-turn whole-stack tester: a script of turns (user message plus
    canned reply) through the real cantrip passes, with store state threaded
    in memory. Nothing persists -- no rows in chat_data / user_data /
    cantrip_data, dynamic-lorebook ops drop, Passenger calls are
    sandbox-flagged. Runs stored cantrips only (real activation; optionally
    narrowed with only_cantrip_ids)."""
    from app.services.sequence_tester import run_sequence

    result = await run_sequence(
        current_user.id,
        conversation_id=req.conversation_id,
        user_name=req.user_name,
        model=req.model,
        initial_messages=(
            [m.model_dump() for m in req.initial_messages]
            if req.initial_messages is not None else None
        ),
        seed_chat_data=req.seed_chat_data,
        seed_user_data=req.seed_user_data,
        seed_cantrip_data=req.seed_cantrip_data,
        start_turn_index=req.start_turn_index,
        only_cantrip_ids=set(req.only_cantrip_ids) if req.only_cantrip_ids else None,
        turns=[
            {
                **t.model_dump(),
                "messages_override": (
                    [m.model_dump() for m in t.messages_override]
                    if t.messages_override is not None else None
                ),
            }
            for t in req.turns
        ],
    )
    return SequenceTestResponse(**result)


@router.get("/{cantrip_id}/export")
async def export_cantrip(
    cantrip_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Download one cantrip as JSON, in the same shape a content pack publishes.

    Export-shaped by design: local wiring (tag, is_active, run_* routing flags)
    stays behind, because a file that travels between installs should not carry
    it. The History panel holds full-fidelity copies for restore.
    """
    import json as _json

    from fastapi.responses import Response

    c = await get_owned_or_404(db, Cantrip, cantrip_id, current_user.id, "Cantrip")

    data = {
        "name": c.name,
        "description": c.description or "",
        "llm_instructions": c.llm_instructions or "",
        "code": c.code or "",
        "reserved_tokens": getattr(c, "reserved_tokens", 0) or 0,
        "user_data_prefixes": _parse_prefixes(getattr(c, "user_data_prefixes", None)),
        "card_edits_standing": bool(getattr(c, "card_edits_standing", False)),
        "author": "",
        "version": "1.0.0",
        "updated": c.updated_at.isoformat() if c.updated_at else "",
    }
    slug = c.name.lower().replace(" ", "_")[:64] or "cantrip"
    return Response(
        content=_json.dumps(data, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{slug}.json"'},
    )
