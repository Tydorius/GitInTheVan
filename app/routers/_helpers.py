"""Shared ownership and tag-uniqueness helpers for resource routers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def check_tag_unique(
    db: AsyncSession,
    model: type[Any],
    user_id: str,
    tag: str,
    exclude_id: str | None = None,
) -> None:
    """Raise 409 if another row of ``model`` owned by ``user_id`` already uses ``tag``."""
    if not tag:
        return
    stmt = select(model.id).where(model.tag == tag, model.user_id == user_id)
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    result = await db.execute(stmt)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tag already in use",
        )


async def get_owned_or_404[T](
    db: AsyncSession,
    model: type[T],
    resource_id: str,
    user_id: str,
    label: str = "Resource",
    options: Sequence[Any] = (),
) -> T:
    """Fetch ``model`` by id and owner, or raise 404."""
    stmt = select(model).where(model.id == resource_id, model.user_id == user_id)
    if options:
        stmt = stmt.options(*options)
    result = await db.execute(stmt)
    obj = result.scalar_one_or_none()
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label} not found",
        )
    return obj


async def assert_owned_endpoint_id(
    db: AsyncSession,
    endpoint_id: str | None,
    user_id: str,
) -> str | None:
    """Resolve a user-supplied endpoint id to an id owned by ``user_id``, else 404.

    Any call made through a named endpoint is sent with that endpoint's
    stored API key, so an endpoint belonging to another user must never be
    accepted (project rule: no user data crosses user boundaries). An empty
    value clears the setting and returns ``None``.
    """
    from app.models.endpoint import Endpoint

    cleaned = (endpoint_id or "").strip()
    if not cleaned:
        return None
    owned = (
        await db.execute(
            select(Endpoint.id).where(
                Endpoint.id == cleaned,
                Endpoint.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if owned is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Endpoint not found",
        )
    return cleaned
