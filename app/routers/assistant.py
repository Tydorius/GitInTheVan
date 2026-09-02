"""The Assistant Pane's own API.

Everything under `/api/assistant` is on the assistant's hard-deny list, so the
assistant can read and change content, configuration and its own conversations,
but never its own permissions.

The two SSE routes take the raw bearer credential as well as the resolved user,
because the executor reaches the management API in-process as the caller and
needs the token itself. Nothing else on this router does.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import bearer_scheme, get_current_user
from app.models.endpoint import Endpoint
from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.admin import get_admin_settings
from app.services.assistant import loop, store
from app.services.assistant.context import ToolContext
from app.services.assistant.permissions import (
    effective,
    group_mode,
    parse_prefs,
    tool_mode,
    validate_prefs,
)
from app.services.assistant.registry import GROUPS, visible_tools
from app.services.assistant.schema import tool_schema
from app.services.llm_params import ParameterDef, params_from_api, params_to_api

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/assistant", tags=["assistant"])

# A single user message. Well past anything a person types, and small enough
# that a runaway paste cannot become a stored conversation nobody can load.
MAX_MESSAGE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ConfigResponse(BaseModel):
    endpoint_id: str | None
    model: str
    parameters: list[ParameterDef] = Field(default_factory=list)
    context_tokens: int
    enabled: bool
    endpoint_role_tag: str = ""
    endpoint_name: str = ""


class ConfigUpdate(BaseModel):
    endpoint_id: str | None = None
    model: str | None = None
    parameters: list[ParameterDef] | None = None
    context_tokens: int | None = None


class PermissionsUpdate(BaseModel):
    groups: dict[str, str] = Field(default_factory=dict)
    tools: dict[str, str] = Field(default_factory=dict)


class ConversationPatch(BaseModel):
    title: str | None = None
    yolo: bool | None = None
    last_route: str | None = None


class MessageRequest(BaseModel):
    content: str
    route: dict[str, Any] | None = None


class ResumeRequest(BaseModel):
    call_id: str
    decision: str | None = None
    note: str | None = None
    client_result: Any | None = None
    route: dict[str, Any] | None = None


class ForkRequest(BaseModel):
    summary: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_or_create_settings(db: AsyncSession, user_id: str) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    row = result.scalar_one_or_none()
    if row is None:
        row = UserSettings(user_id=user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _build_context(
    request: Request,
    user: User,
    token: str,
    conversation_id: str,
    route: dict[str, Any] | None,
    db: AsyncSession,
) -> ToolContext:
    """Assemble the turn context, including the keys to scrub from results."""
    admin = await get_admin_settings()
    result = await db.execute(
        select(Endpoint.api_key).where(Endpoint.user_id == user.id, Endpoint.api_key != "")
    )
    secrets = tuple(sorted({row[0] for row in result.fetchall() if row[0]}))
    return ToolContext(
        user_id=user.id,
        is_admin=bool(user.is_admin),
        bearer_token=token,
        app=request.app,
        conversation_id=conversation_id,
        admin=admin,
        secrets=secrets,
        route=route or {},
    )


def _frame(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream(events) -> StreamingResponse:
    async def generator():
        async for name, data in events:
            yield _frame(name, data)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _error_stream(code: str, message: str) -> StreamingResponse:
    async def generator():
        yield _frame("error", {"code": code, "message": message})
        yield _frame("done", {"reason": "error"})

    return StreamingResponse(generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@router.get("/config", response_model=ConfigResponse)
async def get_config(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user_settings = await _get_or_create_settings(db, current_user.id)
    admin = await get_admin_settings()

    role_tag = ""
    endpoint_name = ""
    if user_settings.assistant_endpoint_id:
        result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == user_settings.assistant_endpoint_id,
                Endpoint.user_id == current_user.id,
            )
        )
        endpoint = result.scalar_one_or_none()
        if endpoint is not None:
            role_tag = endpoint.role_tag or ""
            endpoint_name = endpoint.name

    return ConfigResponse(
        endpoint_id=user_settings.assistant_endpoint_id,
        model=user_settings.assistant_model,
        parameters=params_to_api(user_settings.assistant_parameters_json),
        context_tokens=user_settings.assistant_context_tokens,
        enabled=bool(admin.assistant_enabled),
        endpoint_role_tag=role_tag,
        endpoint_name=endpoint_name,
    )


@router.put("/config", response_model=ConfigResponse)
async def update_config(
    payload: ConfigUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    user_settings = await _get_or_create_settings(db, current_user.id)

    if payload.endpoint_id is not None:
        if payload.endpoint_id == "":
            user_settings.assistant_endpoint_id = None
        else:
            result = await db.execute(
                select(Endpoint).where(
                    Endpoint.id == payload.endpoint_id,
                    Endpoint.user_id == current_user.id,
                )
            )
            if result.scalar_one_or_none() is None:
                raise HTTPException(status_code=400, detail="Endpoint not found")
            user_settings.assistant_endpoint_id = payload.endpoint_id

    if payload.model is not None:
        user_settings.assistant_model = payload.model
    if payload.parameters is not None:
        user_settings.assistant_parameters_json = params_from_api(
            payload.parameters, "user settings (assistant)"
        )
    if payload.context_tokens is not None:
        user_settings.assistant_context_tokens = max(4000, int(payload.context_tokens))

    await db.commit()
    await db.refresh(user_settings)
    return await get_config(current_user, db)


# ---------------------------------------------------------------------------
# Permissions and catalog
# ---------------------------------------------------------------------------


@router.get("/permissions")
async def get_permissions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """The security grid: every group, its mode, and each tool's effective mode.

    `effective` here is computed with no session allowances and yolo off, so
    the grid shows the standing decision rather than whatever a particular
    conversation has been talked into.
    """
    user_settings = await _get_or_create_settings(db, current_user.id)
    admin = await get_admin_settings()
    prefs = parse_prefs(user_settings.assistant_permissions_json)

    ctx = ToolContext(
        user_id=current_user.id,
        is_admin=bool(current_user.is_admin),
        bearer_token="",
        app=None,
        conversation_id="",
        admin=admin,
    )
    visible = {tool.name for tool in visible_tools(ctx)}

    from app.services.assistant.registry import TOOLS

    groups: list[dict[str, Any]] = []
    for key, group in GROUPS.items():
        disabled = bool(group.admin_flag) and not bool(getattr(admin, group.admin_flag, False))
        tools: list[dict[str, Any]] = []
        for tool in TOOLS.values():
            if tool.group != key:
                continue
            if tool.admin_only and tool.name not in visible:
                continue
            tools.append(
                {
                    "name": tool.name,
                    "summary": tool.summary,
                    "risk": str(tool.risk),
                    "mode": tool_mode(tool, prefs),
                    "effective": effective(
                        tool, group, prefs, session_allows=set(), yolo=False
                    ),
                }
            )
        groups.append(
            {
                "key": key,
                "label": group.label,
                "page": group.route,
                "category": group.category,
                "default_mode": group.default_mode,
                "disabled_by_admin": disabled,
                "mode": group_mode(group, prefs),
                "tools": sorted(tools, key=lambda t: t["name"]),
            }
        )

    return {"groups": groups}


@router.put("/permissions")
async def update_permissions(
    payload: PermissionsUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    prefs = {"groups": dict(payload.groups), "tools": dict(payload.tools)}
    errors = validate_prefs(prefs)
    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    user_settings = await _get_or_create_settings(db, current_user.id)
    user_settings.assistant_permissions_json = json.dumps(prefs)
    await db.commit()
    return await get_permissions(current_user, db)


@router.get("/catalog")
async def get_catalog(
    current_user: Annotated[User, Depends(get_current_user)],
):
    admin = await get_admin_settings()
    ctx = ToolContext(
        user_id=current_user.id,
        is_admin=bool(current_user.is_admin),
        bearer_token="",
        app=None,
        conversation_id="",
        admin=admin,
    )
    tools = visible_tools(ctx)
    return {
        "tools": [
            {
                "name": tool.name,
                "group": tool.group,
                "risk": str(tool.risk),
                "kind": tool.kind,
                "summary": tool.summary,
                "doc": tool.doc,
                "schema": tool_schema(tool),
            }
            for tool in tools
        ]
    }


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations(
    current_user: Annotated[User, Depends(get_current_user)],
):
    return {"conversations": await store.list_conversations(current_user.id)}


@router.post("/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    current_user: Annotated[User, Depends(get_current_user)],
):
    admin = await get_admin_settings()
    row, rotated = await store.create(current_user.id, admin.max_assistant_conversations)
    payload = store.to_dict(row)
    payload["rotated_title"] = rotated
    return payload


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    row = await store.get(current_user.id, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return store.to_dict(row)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: str,
    payload: ConversationPatch,
    current_user: Annotated[User, Depends(get_current_user)],
):
    row = await store.patch(
        current_user.id,
        conversation_id,
        title=payload.title,
        yolo=payload.yolo,
        last_route=payload.last_route,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return store.to_dict(row)


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    if not await store.delete(current_user.id, conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found")


@router.post("/conversations/{conversation_id}/save")
async def save_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    row = await store.set_saved(current_user.id, conversation_id, True)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return store.to_dict(row, include_messages=False)


@router.delete("/conversations/{conversation_id}/save")
async def unsave_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
):
    row = await store.set_saved(current_user.id, conversation_id, False)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return store.to_dict(row, include_messages=False)


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    fmt: Annotated[str, Query(alias="format")] = "json",
):
    rendered = await store.export(current_user.id, conversation_id, fmt)
    if rendered is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    content, filename, media_type = rendered
    from fastapi.responses import Response

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _summarize_for_fork(user_id: str, conversation_id: str) -> str:
    """Summarize a conversation with the user's own assistant endpoint.

    Reuses the compactor's summarizer, so a fork reads the same way a compacted
    conversation does. Any failure yields an empty summary rather than an
    error: the fork is a convenience and must not depend on the upstream.
    """
    from app.services.assistant import compaction
    from app.services.assistant.llm import resolve_assistant_candidate

    source = await store.get(user_id, conversation_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    resolved = await resolve_assistant_candidate(user_id)
    if resolved is None:
        return ""
    candidate, model, user_layer = resolved
    data = store.to_dict(source)
    prior = str((data.get("compaction") or {}).get("summary") or "")
    try:
        summary = await compaction.summarize(candidate, model, user_layer, data.get("messages") or [], prior)
    except Exception:  # noqa: BLE001 - a fork must never fail on the upstream
        logger.exception("assistant fork summary failed")
        return ""
    return summary or ""


@router.post("/conversations/{conversation_id}/fork", status_code=status.HTTP_201_CREATED)
async def fork_conversation(
    conversation_id: str,
    payload: ForkRequest,
    current_user: Annotated[User, Depends(get_current_user)],
):
    summary = payload.summary.strip()
    if not summary:
        summary = await _summarize_for_fork(current_user.id, conversation_id)
    forked = await store.fork_from_summary(current_user.id, conversation_id, summary)
    if forked is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    row, rotated = forked
    out = store.to_dict(row)
    out["rotated_title"] = rotated
    return out


# ---------------------------------------------------------------------------
# The turn (SSE)
# ---------------------------------------------------------------------------


@router.post("/conversations/{conversation_id}/message")
async def post_message(
    conversation_id: str,
    payload: MessageRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    admin = await get_admin_settings()
    if not admin.assistant_enabled:
        raise HTTPException(status_code=403, detail="The assistant is disabled by the administrator")

    if len(payload.content.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise HTTPException(status_code=413, detail="Message too large")

    row = await store.get(current_user.id, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    ctx = await _build_context(
        request, current_user, credentials.credentials, conversation_id, payload.route, db
    )
    return _stream(loop.run_turn(ctx, user_message=payload.content))


@router.post("/conversations/{conversation_id}/resume")
async def post_resume(
    conversation_id: str,
    payload: ResumeRequest,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    admin = await get_admin_settings()
    if not admin.assistant_enabled:
        raise HTTPException(status_code=403, detail="The assistant is disabled by the administrator")

    row = await store.get(current_user.id, conversation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    ctx = await _build_context(
        request, current_user, credentials.credentials, conversation_id, payload.route, db
    )
    decision: dict[str, Any] = {"call_id": payload.call_id}
    if payload.client_result is not None:
        decision["client_result"] = payload.client_result
    if payload.decision is not None:
        decision["decision"] = payload.decision
    if payload.note is not None:
        decision["note"] = payload.note
    return _stream(loop.run_turn(ctx, decision=decision))
