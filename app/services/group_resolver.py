"""Resolve tag groups and expand member tags for pipeline activation."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.cantrip import Cantrip
from app.models.lorebook import Lorebook
from app.models.tag_group import TagGroup

logger = logging.getLogger(__name__)


async def resolve_group_tags(
    db: AsyncSession,
    user_id: str,
    tags: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve active tag groups and return expanded tag list plus activated group names.

    Returns:
        (expanded_tags, activated_group_names)

    expanded_tags is the original tags list plus any new virtual tags
    from group members. Group members' resource tags are injected as
    standard typed tags so should_activate_resource picks them up.
    """
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.user_id == user_id)
        .options(selectinload(TagGroup.members))
    )
    groups = result.scalars().all()

    if not groups:
        return tags, []

    message_tag_names = {
        t.get("name") for t in tags
        if t.get("type") in ("taggroup", "unknown")
    }

    activated_groups: list[TagGroup] = []
    for group in groups:
        if group.is_active:
            activated_groups.append(group)
        elif group.tag and group.tag in message_tag_names:
            activated_groups.append(group)

    if not activated_groups:
        return tags, []

    member_ids_by_type: dict[str, set[str]] = {"lorebook": set(), "cantrip": set()}
    for group in activated_groups:
        for member in group.members:
            member_ids_by_type.setdefault(member.member_type, set()).add(member.member_id)

    expanded = list(tags)
    existing_keys = {(t.get("type"), t.get("name")) for t in expanded}
    activated_group_names = [g.name for g in activated_groups]

    if member_ids_by_type.get("lorebook"):
        lb_result = await db.execute(
            select(Lorebook.id, Lorebook.tag).where(
                Lorebook.id.in_(member_ids_by_type["lorebook"]),
                Lorebook.user_id == user_id,
            )
        )
        for lb_id, lb_tag in lb_result.fetchall():
            if lb_id not in member_ids_by_type["lorebook"]:
                continue
            member_ids_by_type["lorebook"].discard(lb_id)
            if lb_tag and ("lore", lb_tag) not in existing_keys:
                expanded.append({"type": "lore", "name": lb_tag, "owner": None, "raw": f"lore-{lb_tag}"})
                existing_keys.add(("lore", lb_tag))

    if member_ids_by_type.get("cantrip"):
        ct_result = await db.execute(
            select(Cantrip.id, Cantrip.tag).where(
                Cantrip.id.in_(member_ids_by_type["cantrip"]),
                Cantrip.user_id == user_id,
            )
        )
        for ct_id, ct_tag in ct_result.fetchall():
            if ct_id not in member_ids_by_type["cantrip"]:
                continue
            member_ids_by_type["cantrip"].discard(ct_id)
            if ct_tag and ("cantrip", ct_tag) not in existing_keys:
                expanded.append({"type": "cantrip", "name": ct_tag, "owner": None, "raw": f"cantrip-{ct_tag}"})
                existing_keys.add(("cantrip", ct_tag))

    for mtype, remaining_ids in member_ids_by_type.items():
        for missing_id in remaining_ids:
            logger.warning(
                "Tag group member %s '%s' not found for user %s, skipping",
                mtype, missing_id, user_id[:8],
            )

    return expanded, activated_group_names
