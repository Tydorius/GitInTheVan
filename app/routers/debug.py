import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.services.debug import (
    clear_exchanges,
    delete_exchange,
    get_exchange,
    list_exchanges,
    set_label,
    set_saved,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/debug", tags=["debug"])

# Four columns is the documented ceiling: past that the columns are too narrow to
# read a diff in, and the baseline radio stops being a quick comparison.
MAX_COMPARE_RUNS = 4


class DebugRunTotals(BaseModel):
    """Token, latency and efficiency figures for one run.

    Every field is optional because a schema-1 exchange predates the run block
    and a run whose upstream returned no usage carries only some of them.
    """

    llm_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    injected_tokens: int = 0
    tokens_source: str = ""
    llm_latency_ms: float = 0.0
    total_latency_ms: float | None = None
    overhead_ms: float | None = None
    tokens_per_second: float | None = None
    injection_overhead_pct: float | None = None


class DebugExchangeListItem(BaseModel):
    id: str
    chat_id: str
    model: str
    label: str
    saved: bool
    saved_at: str
    created_at: str
    has_response: bool
    has_verification: bool
    stage_count: int
    source: str
    totals: DebugRunTotals


class DebugExchangeResponse(BaseModel):
    id: str
    chat_id: str
    model: str
    label: str
    saved: bool
    saved_at: str
    pipeline_data: dict
    response_content: str
    verification_data: dict
    created_at: str


class DebugListResponse(BaseModel):
    exchanges: list[DebugExchangeListItem]
    saved_count: int
    max_saved: int


class SaveRunRequest(BaseModel):
    label: str = ""


class LabelRequest(BaseModel):
    label: str


class CompareRequest(BaseModel):
    ids: list[str]
    baseline_id: str = ""


def _list_item(e: dict) -> DebugExchangeListItem:
    pipeline = e.get("pipeline_data", {})
    run = pipeline.get("run", {})
    verification = e.get("verification_data") or {}
    return DebugExchangeListItem(
        id=e["id"],
        chat_id=e["chat_id"],
        model=e["model"],
        label=e.get("label", "") or _auto_label(e),
        saved=e.get("saved", False),
        saved_at=e.get("saved_at", ""),
        created_at=e["created_at"],
        has_response=bool(e.get("response_content")),
        # "approved" in the payload, not a truthy dict: the map path used to
        # pass {} here, so this flag was false on every map run.
        has_verification="approved" in verification,
        stage_count=len(pipeline.get("stages", [])),
        source=run.get("source", "live"),
        totals=DebugRunTotals(**run.get("totals", {})),
    )


def _auto_label(e: dict) -> str:
    """Derive a readable name from what the run activated.

    Without this the picker is a column of identical timestamps, and choosing a
    comparison baseline means opening each one to find out what it was.
    """
    pipeline = e.get("pipeline_data", {})
    parts: list[str] = []

    for stage in pipeline.get("stages", []):
        if stage.get("name") == "map_stage" and stage.get("metadata", {}).get("map_name"):
            parts.append(f"map:{stage['metadata']['map_name']}")
            break

    fired = [c for c in pipeline.get("run", {}).get("cantrips", []) if c.get("triggered")]
    if len(fired) == 1:
        parts.append(fired[0]["name"])
    elif fired:
        parts.append(f"{len(fired)} cantrips")

    if not parts:
        tags = pipeline.get("tags", [])
        if tags:
            parts.append(tags[0])

    return " + ".join(parts)


@router.get("", response_model=DebugListResponse)
async def list_debug_exchanges(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    saved_only: bool = False,
):
    from app.services.admin import get_caps

    exchanges = await list_exchanges(current_user.id, saved_only=saved_only)
    caps = await get_caps()
    return DebugListResponse(
        exchanges=[_list_item(e) for e in exchanges],
        saved_count=sum(1 for e in exchanges if e.get("saved")),
        max_saved=caps.get("max_saved_debug_runs", 10),
    )


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


@router.post("/{exchange_id}/save", response_model=DebugExchangeResponse)
async def save_debug_exchange(
    exchange_id: str,
    req: SaveRunRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Pin a run so the rolling retention prune cannot evict it."""
    refusal = await set_saved(current_user.id, exchange_id, True, req.label)
    if refusal:
        code = (
            status.HTTP_404_NOT_FOUND if refusal == "Run not found"
            else status.HTTP_409_CONFLICT
        )
        raise HTTPException(status_code=code, detail=refusal)
    return DebugExchangeResponse(**await get_exchange(current_user.id, exchange_id))


@router.delete("/{exchange_id}/save", response_model=DebugExchangeResponse)
async def unsave_debug_exchange(
    exchange_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    refusal = await set_saved(current_user.id, exchange_id, False)
    if refusal:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=refusal)
    return DebugExchangeResponse(**await get_exchange(current_user.id, exchange_id))


@router.patch("/{exchange_id}", response_model=DebugExchangeResponse)
async def rename_debug_exchange(
    exchange_id: str,
    req: LabelRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not await set_label(current_user.id, exchange_id, req.label):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug exchange not found")
    return DebugExchangeResponse(**await get_exchange(current_user.id, exchange_id))


@router.delete("/{exchange_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_debug_exchange(
    exchange_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not await delete_exchange(current_user.id, exchange_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug exchange not found")


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_debug_exchanges(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    include_saved: bool = False,
):
    """Clear runs. Saved runs survive unless explicitly included."""
    await clear_exchanges(current_user.id, include_saved=include_saved)


class ReplayResponse(BaseModel):
    run_id: str
    warning: str


@router.post("/{exchange_id}/replay", response_model=ReplayResponse)
async def replay_debug_exchange(
    exchange_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Re-run this exchange's original messages through the current config.

    Returns the id of the new run, which the UI selects as the comparison
    partner of the source run.
    """
    from app.services.debug_replay import ReplayError, replay_exchange

    try:
        run_id = await replay_exchange(current_user.id, exchange_id)
    except ReplayError as exc:
        code = (
            status.HTTP_404_NOT_FOUND if str(exc) == "Run not found"
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=str(exc)) from exc

    return ReplayResponse(
        run_id=run_id,
        # Stated plainly rather than implying an isolation replay does not have.
        warning=(
            "Cantrip side effects are not sandboxed: anything that writes user "
            "or cantrip data, rolls dice, or advances a counter has run again."
        ),
    )


class SandboxResponse(BaseModel):
    id: str
    name: str
    source_exchange_id: str
    source_chat_id: str
    sandbox_chat_id: str
    message_list: list[dict]
    message_count: int
    model: str
    run_count: int
    last_run_at: str
    created_at: str


class SandboxListResponse(BaseModel):
    sandboxes: list[SandboxResponse]


class CreateSandboxRequest(BaseModel):
    name: str = ""


class SandboxMessagesRequest(BaseModel):
    messages: list[dict]


class SandboxRunResponse(BaseModel):
    run_id: str
    run_count: int


def _sandbox_response(data: dict) -> SandboxResponse:
    return SandboxResponse(**{k: v for k, v in data.items() if k != "messages"})


def _sandbox_error(exc: Exception) -> HTTPException:
    detail = str(exc)
    code = (
        status.HTTP_404_NOT_FOUND
        if detail in ("Sandbox not found", "Run not found")
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(status_code=code, detail=detail)


@router.get("/sandboxes/list", response_model=SandboxListResponse)
async def list_debug_sandboxes(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.debug_sandbox import list_sandboxes

    return SandboxListResponse(
        sandboxes=[_sandbox_response(s) for s in await list_sandboxes(current_user.id)]
    )


@router.post("/{exchange_id}/sandbox", response_model=SandboxResponse)
async def create_debug_sandbox(
    exchange_id: str,
    req: CreateSandboxRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Fork this run into a sandbox that can be re-fired from inside GitInTheVan.

    The request and the conversation state it read are copied. Firing the
    sandbox never touches the conversation it came from, and never contacts the
    chat service the original request arrived from.
    """
    from app.services.debug_sandbox import SandboxError, create_sandbox

    try:
        return _sandbox_response(await create_sandbox(current_user.id, exchange_id, req.name))
    except SandboxError as exc:
        raise _sandbox_error(exc) from exc


@router.post("/sandboxes/{sandbox_id}/run", response_model=SandboxRunResponse)
async def run_debug_sandbox(
    sandbox_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.debug_sandbox import SandboxError, get_sandbox, run_sandbox

    try:
        run_id = await run_sandbox(current_user.id, sandbox_id)
    except SandboxError as exc:
        raise _sandbox_error(exc) from exc

    sandbox = await get_sandbox(current_user.id, sandbox_id)
    return SandboxRunResponse(run_id=run_id, run_count=sandbox["run_count"] if sandbox else 0)


@router.post("/sandboxes/{sandbox_id}/reset", response_model=SandboxResponse)
async def reset_debug_sandbox(
    sandbox_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Discard what this sandbox's runs accumulated and re-fork from the source."""
    from app.services.debug_sandbox import SandboxError, reset_sandbox

    try:
        return _sandbox_response(await reset_sandbox(current_user.id, sandbox_id))
    except SandboxError as exc:
        raise _sandbox_error(exc) from exc


@router.patch("/sandboxes/{sandbox_id}", response_model=SandboxResponse)
async def update_debug_sandbox(
    sandbox_id: str,
    req: SandboxMessagesRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Edit the request this sandbox sends."""
    from app.services.debug_sandbox import get_sandbox, update_messages

    if not await update_messages(current_user.id, sandbox_id, req.messages):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")
    return _sandbox_response(await get_sandbox(current_user.id, sandbox_id))


@router.delete("/sandboxes/{sandbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_debug_sandbox(
    sandbox_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from app.services.debug_sandbox import delete_sandbox

    if not await delete_sandbox(current_user.id, sandbox_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")


@router.get("/{exchange_id}/export")
async def export_debug_exchange(
    exchange_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    format: str = "json",
):
    """Export one run as JSON or Markdown.

    JSON is lossless -- the whole trace including the run block -- so it can be
    re-read later or attached to a bug report.
    """
    from app.services.debug_export import render_exchange

    if format not in ("json", "markdown"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format must be 'json' or 'markdown'",
        )

    exchange = await get_exchange(current_user.id, exchange_id)
    if not exchange:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Debug exchange not found")

    content, media_type, filename = render_exchange(exchange, format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/compare")
async def compare_debug_exchanges(
    req: CompareRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Compare 2-4 runs against a baseline.

    Diffing happens server-side so the screen, the Markdown export and the JSON
    export all read one implementation, and so it can be unit-tested.
    """
    from app.services.debug_compare import compare_runs

    runs = await _load_for_compare(current_user.id, req.ids)
    baseline_id = req.baseline_id or req.ids[0]
    if baseline_id not in {r["id"] for r in runs}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="baseline_id must be one of the compared runs",
        )
    return compare_runs(runs, baseline_id)


@router.post("/compare/export")
async def export_comparison(
    req: CompareRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    format: str = "markdown",
):
    from app.services.debug_compare import compare_runs
    from app.services.debug_export import render_comparison

    if format not in ("json", "markdown"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="format must be 'json' or 'markdown'",
        )

    runs = await _load_for_compare(current_user.id, req.ids)
    baseline_id = req.baseline_id or req.ids[0]
    result = compare_runs(runs, baseline_id)

    content, media_type, filename = render_comparison(runs, result, format)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _load_for_compare(user_id: str, ids: list[str]) -> list[dict]:
    """Load the requested runs, rejecting a selection that cannot be compared."""
    if len(ids) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least two runs to compare",
        )
    if len(ids) > MAX_COMPARE_RUNS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_COMPARE_RUNS} runs can be compared at once",
        )
    if len(set(ids)) != len(ids):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The same run was selected more than once",
        )

    runs = []
    for exchange_id in ids:
        exchange = await get_exchange(user_id, exchange_id)
        if not exchange:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Debug exchange {exchange_id} not found",
            )
        runs.append(exchange)
    return runs
