import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.llm_params import ParameterDef, params_from_api, params_to_api
from app.services.pipeline_order import MAX_PLAN_ITEMS, parse_plan
from app.services.response_tags import parse_strip_patterns

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


async def _passenger_effective_cap(user_settings: UserSettings) -> int:
    """min(admin ceiling, user budget when set) -- the rule-22 clamp, shown
    next to the budget field so the effective cap is visible."""
    from app.services.admin import get_admin_settings

    admin = await get_admin_settings()
    cap = max(0, admin.max_passenger_calls_per_request)
    budget = user_settings.passenger_call_budget
    if budget and budget > 0:
        cap = min(cap, budget)
    return cap


class SettingsResponse(BaseModel):
    default_endpoint_id: str | None
    default_model: str
    preserve_thinking: bool
    gitv_status: bool
    simulated_streaming_speed: int
    driver_callable_turns: int
    bypass_method: str
    prefill_enabled: bool
    streaming_post_processing_enabled: bool
    context_budget_percent: float
    context_window_override: int
    summarization_block_size: int = 0
    cache_friendly_mode: bool = False
    auto_cache_control: bool = False
    debug_mode: bool
    default_map_id: str | None
    # Phase 29: lexicon compiler settings and the user half of the
    # doubly-gated cantrip auto-install.
    lexicon_enabled: bool = True
    lexicon_endpoint_id: str | None = None
    cantrip_auto_install_enabled: bool = False
    # Phase 17: Passenger scripted LLM calls. passenger_call_budget is the
    # user half of the rule-22 cap: 0 = no self-imposed limit, effective =
    # min(admin cap, user budget).
    passenger_calling_enabled: bool = True
    passenger_call_budget: int = 0
    # Read-only: the effective per-request Passenger call cap after the
    # rule-22 clamp (min of the admin ceiling and this user's budget when
    # set). Surfaced so the clamp is visible where the budget is edited.
    passenger_effective_cap: int = 0
    # One parameter set per LLM role, matching the three model fields. These are
    # the blanket layer: broader than the endpoint, narrower than the client.
    parameters: list[ParameterDef] = Field(default_factory=list)
    verification_parameters: list[ParameterDef] = Field(default_factory=list)
    summarization_parameters: list[ParameterDef] = Field(default_factory=list)
    lexicon_parameters: list[ParameterDef] = Field(default_factory=list)
    passenger_parameters: list[ParameterDef] = Field(default_factory=list)
    # Package 3 (item 22): regex patterns stripped from every final reply.
    response_strip_patterns: list[str] = Field(default_factory=list)
    # Pipeline ordering (0.41.0): {"enabled": bool, "items": [{"type":
    # "cantrip"|"lorebook", "id": str}]} -- the manual pre-forward order.
    # Empty dict = manual ordering off.
    pipeline_order: dict = Field(default_factory=dict)


class SettingsUpdate(BaseModel):
    default_endpoint_id: str | None = None
    default_model: str | None = None
    preserve_thinking: bool | None = None
    gitv_status: bool | None = None
    simulated_streaming_speed: int | None = None
    driver_callable_turns: int | None = None
    bypass_method: str | None = None
    prefill_enabled: bool | None = None
    streaming_post_processing_enabled: bool | None = None
    context_budget_percent: float | None = None
    context_window_override: int | None = None
    summarization_block_size: int | None = Field(default=None, ge=0)
    cache_friendly_mode: bool | None = None
    auto_cache_control: bool | None = None
    debug_mode: bool | None = None
    default_map_id: str | None = None
    lexicon_enabled: bool | None = None
    lexicon_endpoint_id: str | None = None
    cantrip_auto_install_enabled: bool | None = None
    passenger_calling_enabled: bool | None = None
    passenger_call_budget: int | None = None
    parameters: list[ParameterDef] | None = None
    verification_parameters: list[ParameterDef] | None = None
    summarization_parameters: list[ParameterDef] | None = None
    lexicon_parameters: list[ParameterDef] | None = None
    passenger_parameters: list[ParameterDef] | None = None
    response_strip_patterns: list[str] | None = None
    pipeline_order: dict | None = None


@router.get("")
async def get_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id)
        db.add(user_settings)
        await db.commit()
        await db.refresh(user_settings)

    return SettingsResponse(
        default_endpoint_id=user_settings.default_endpoint_id,
        default_model=user_settings.default_model,
        preserve_thinking=user_settings.preserve_thinking,
        gitv_status=user_settings.gitv_status,
        simulated_streaming_speed=user_settings.simulated_streaming_speed,
        driver_callable_turns=user_settings.driver_callable_turns,
        bypass_method=user_settings.bypass_method,
        prefill_enabled=user_settings.prefill_enabled,
        streaming_post_processing_enabled=user_settings.streaming_post_processing_enabled,
        context_budget_percent=user_settings.context_budget_percent,
        context_window_override=user_settings.context_window_override,
        summarization_block_size=user_settings.summarization_block_size,
        cache_friendly_mode=user_settings.cache_friendly_mode,
        auto_cache_control=user_settings.auto_cache_control,
        debug_mode=user_settings.debug_mode,
        default_map_id=user_settings.default_map_id,
        lexicon_enabled=user_settings.lexicon_enabled,
        lexicon_endpoint_id=user_settings.lexicon_endpoint_id,
        cantrip_auto_install_enabled=user_settings.cantrip_auto_install_enabled,
        passenger_calling_enabled=user_settings.passenger_calling_enabled,
        passenger_call_budget=user_settings.passenger_call_budget,
        passenger_effective_cap=await _passenger_effective_cap(user_settings),
        parameters=params_to_api(user_settings.parameters_json),
        verification_parameters=params_to_api(user_settings.verification_parameters_json),
        summarization_parameters=params_to_api(user_settings.summarization_parameters_json),
        lexicon_parameters=params_to_api(user_settings.lexicon_parameters_json),
        passenger_parameters=params_to_api(user_settings.passenger_parameters_json),
        response_strip_patterns=parse_strip_patterns(
            getattr(user_settings, "response_strip_patterns", None)
        ),
        pipeline_order=parse_plan(getattr(user_settings, "pipeline_order_json", None)),
    )


@router.put("")
async def update_settings(
    req: SettingsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == current_user.id))
    user_settings = result.scalar_one_or_none()
    if user_settings is None:
        user_settings = UserSettings(user_id=current_user.id)
        db.add(user_settings)

    if "default_endpoint_id" in req.model_fields_set:
        if req.default_endpoint_id:
            from app.models.endpoint import Endpoint
            ep = (await db.execute(
                select(Endpoint.id).where(
                    Endpoint.id == req.default_endpoint_id,
                    Endpoint.user_id == current_user.id,
                )
            )).scalar_one_or_none()
            if ep is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
            user_settings.default_endpoint_id = req.default_endpoint_id
        else:
            user_settings.default_endpoint_id = None
    if req.default_model is not None:
        user_settings.default_model = req.default_model
    if req.preserve_thinking is not None:
        user_settings.preserve_thinking = req.preserve_thinking
    if req.gitv_status is not None:
        user_settings.gitv_status = req.gitv_status
    if req.simulated_streaming_speed is not None:
        user_settings.simulated_streaming_speed = req.simulated_streaming_speed
    if req.driver_callable_turns is not None:
        user_settings.driver_callable_turns = max(0, req.driver_callable_turns)
    if req.bypass_method is not None:
        user_settings.bypass_method = req.bypass_method
    if req.prefill_enabled is not None:
        user_settings.prefill_enabled = req.prefill_enabled
    if req.streaming_post_processing_enabled is not None:
        user_settings.streaming_post_processing_enabled = req.streaming_post_processing_enabled
    if req.context_budget_percent is not None:
        user_settings.context_budget_percent = max(0.0, min(100.0, req.context_budget_percent))
    if req.context_window_override is not None:
        user_settings.context_window_override = max(0, req.context_window_override)
    if req.summarization_block_size is not None:
        user_settings.summarization_block_size = req.summarization_block_size
    if req.cache_friendly_mode is not None:
        user_settings.cache_friendly_mode = req.cache_friendly_mode
    if req.auto_cache_control is not None:
        user_settings.auto_cache_control = req.auto_cache_control
    if req.debug_mode is not None:
        user_settings.debug_mode = req.debug_mode
    if "default_map_id" in req.model_fields_set:
        if req.default_map_id:
            from app.models.map import Map
            m = (await db.execute(
                select(Map.id).where(
                    Map.id == req.default_map_id,
                    Map.user_id == current_user.id,
                )
            )).scalar_one_or_none()
            if m is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Map not found")
            user_settings.default_map_id = req.default_map_id
        else:
            user_settings.default_map_id = None
    if req.lexicon_enabled is not None:
        user_settings.lexicon_enabled = req.lexicon_enabled
    if "lexicon_endpoint_id" in req.model_fields_set:
        if req.lexicon_endpoint_id:
            from app.models.endpoint import Endpoint
            ep = (await db.execute(
                select(Endpoint.id).where(
                    Endpoint.id == req.lexicon_endpoint_id,
                    Endpoint.user_id == current_user.id,
                )
            )).scalar_one_or_none()
            if ep is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
            user_settings.lexicon_endpoint_id = req.lexicon_endpoint_id
        else:
            user_settings.lexicon_endpoint_id = None
    if req.cantrip_auto_install_enabled is not None:
        user_settings.cantrip_auto_install_enabled = req.cantrip_auto_install_enabled
    if req.passenger_calling_enabled is not None:
        user_settings.passenger_calling_enabled = req.passenger_calling_enabled
    if req.passenger_call_budget is not None:
        user_settings.passenger_call_budget = max(0, req.passenger_call_budget)
    if req.response_strip_patterns is not None:
        from app.services.response_tags import validate_strip_patterns

        try:
            cleaned = validate_strip_patterns(req.response_strip_patterns)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        user_settings.response_strip_patterns = json.dumps(cleaned)
    if req.pipeline_order is not None:
        # Shape-validated and normalized through the same parser the
        # pipeline uses; the ids themselves are resolved at request time
        # (a listed item that has since been deleted simply never matches).
        items = req.pipeline_order.get("items")
        if not isinstance(items, list) or len(items) > MAX_PLAN_ITEMS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"pipeline_order.items must be a list of at most {MAX_PLAN_ITEMS}",
            )
        plan = parse_plan(json.dumps(req.pipeline_order))
        user_settings.pipeline_order_json = (
            json.dumps(plan) if plan["items"] else ""
        )
    if req.parameters is not None:
        user_settings.parameters_json = params_from_api(req.parameters, "driver parameters")
    if req.verification_parameters is not None:
        user_settings.verification_parameters_json = params_from_api(
            req.verification_parameters, "verification parameters"
        )
    if req.summarization_parameters is not None:
        user_settings.summarization_parameters_json = params_from_api(
            req.summarization_parameters, "summarization parameters"
        )
    if req.lexicon_parameters is not None:
        user_settings.lexicon_parameters_json = params_from_api(
            req.lexicon_parameters, "lexicon parameters"
        )
    if req.passenger_parameters is not None:
        user_settings.passenger_parameters_json = params_from_api(
            req.passenger_parameters, "passenger parameters"
        )

    await db.commit()
    await db.refresh(user_settings)

    return SettingsResponse(
        default_endpoint_id=user_settings.default_endpoint_id,
        default_model=user_settings.default_model,
        preserve_thinking=user_settings.preserve_thinking,
        gitv_status=user_settings.gitv_status,
        simulated_streaming_speed=user_settings.simulated_streaming_speed,
        driver_callable_turns=user_settings.driver_callable_turns,
        bypass_method=user_settings.bypass_method,
        prefill_enabled=user_settings.prefill_enabled,
        streaming_post_processing_enabled=user_settings.streaming_post_processing_enabled,
        context_budget_percent=user_settings.context_budget_percent,
        context_window_override=user_settings.context_window_override,
        summarization_block_size=user_settings.summarization_block_size,
        cache_friendly_mode=user_settings.cache_friendly_mode,
        auto_cache_control=user_settings.auto_cache_control,
        debug_mode=user_settings.debug_mode,
        default_map_id=user_settings.default_map_id,
        lexicon_enabled=user_settings.lexicon_enabled,
        lexicon_endpoint_id=user_settings.lexicon_endpoint_id,
        cantrip_auto_install_enabled=user_settings.cantrip_auto_install_enabled,
        passenger_calling_enabled=user_settings.passenger_calling_enabled,
        passenger_call_budget=user_settings.passenger_call_budget,
        passenger_effective_cap=await _passenger_effective_cap(user_settings),
        parameters=params_to_api(user_settings.parameters_json),
        verification_parameters=params_to_api(user_settings.verification_parameters_json),
        summarization_parameters=params_to_api(user_settings.summarization_parameters_json),
        lexicon_parameters=params_to_api(user_settings.lexicon_parameters_json),
        passenger_parameters=params_to_api(user_settings.passenger_parameters_json),
        response_strip_patterns=parse_strip_patterns(
            getattr(user_settings, "response_strip_patterns", None)
        ),
        pipeline_order=parse_plan(getattr(user_settings, "pipeline_order_json", None)),
    )
