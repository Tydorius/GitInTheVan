import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.dependencies import get_current_user
from app.models.user import User
from app.services.metrics import (
    list_llm_call_metrics,
    list_prompt_object_metrics,
    metrics_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


class PromptObjectAggregate(BaseModel):
    object_type: str
    object_id: str
    parent_id: str
    object_name: str
    parent_name: str
    fires: int
    sum_difference: int
    avg_difference: float


class PromptTypeTotal(BaseModel):
    object_type: str
    fires: int
    sum_difference: int


class LlmCallAggregate(BaseModel):
    purpose: str
    endpoint_id: str
    endpoint_name: str
    model: str
    rule_id: str
    rule_name: str
    calls: int
    ok: int
    errors: int
    retries: int
    failovers: int
    mean_latency_ms: float
    median_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    tokens_source_counts: dict[str, int]


class MetricsSummaryResponse(BaseModel):
    window_days: int
    chat_id: str
    include_flagged: bool
    chats: list[str]
    prompt_objects: list[PromptObjectAggregate]
    prompt_type_totals: list[PromptTypeTotal]
    llm_calls: list[LlmCallAggregate]


class PromptObjectRow(BaseModel):
    id: str
    chat_id: str
    context: str
    is_retry: bool
    created_at: str
    object_type: str
    object_id: str
    parent_id: str
    pre_tokens: int
    post_tokens: int
    difference: int


class PromptObjectListResponse(BaseModel):
    rows: list[PromptObjectRow]
    total: int


class LlmCallRow(BaseModel):
    id: str
    chat_id: str
    context: str
    is_retry: bool
    created_at: str
    purpose: str
    endpoint_id: str
    endpoint_name: str
    model: str
    status_code: int | None
    latency_ms: float
    failover_attempt: int
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    tokens_source: str
    rule_id: str


class LlmCallListResponse(BaseModel):
    rows: list[LlmCallRow]
    total: int


@router.get("/summary", response_model=MetricsSummaryResponse)
async def get_metrics_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    chat_id: str = Query("", max_length=64),
    include_flagged: bool = False,
):
    summary = await metrics_summary(
        current_user.id, days=days, chat_id=chat_id, include_flagged=include_flagged
    )
    return MetricsSummaryResponse(**summary)


@router.get("/prompt-objects", response_model=PromptObjectListResponse)
async def get_prompt_object_rows(
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    chat_id: str = Query("", max_length=64),
):
    rows, total = await list_prompt_object_metrics(
        current_user.id, limit=limit, offset=offset, chat_id=chat_id
    )
    return PromptObjectListResponse(
        rows=[PromptObjectRow(**r) for r in rows], total=total
    )


@router.get("/llm-calls", response_model=LlmCallListResponse)
async def get_llm_call_rows(
    current_user: Annotated[User, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    chat_id: str = Query("", max_length=64),
):
    rows, total = await list_llm_call_metrics(
        current_user.id, limit=limit, offset=offset, chat_id=chat_id
    )
    return LlmCallListResponse(rows=[LlmCallRow(**r) for r in rows], total=total)
