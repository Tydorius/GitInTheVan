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
) -> tuple[list[dict[str, Any]], list[str], dict[str, set[str]]]:
    """Resolve active tag groups; return expanded tags, activated group
    names, and direct member ids.

    Returns:
        (expanded_tags, activated_group_names, direct_member_ids)

    expanded_tags is the original tags list plus any new virtual tags
    from group members. Group members' resource tags are injected as
    standard typed tags so should_activate_resource picks them up.

    direct_member_ids maps member_type -> resource ids for members of
    ACTIVATED groups whose ``activate_members`` flag is on -- the seventh
    activation source (rule 19): membership turns a member on regardless
    of its own tag or Active flag. Opt-in per group; groups without the
    flag keep the macro semantics and contribute nothing here. Union-only:
    the ids can only ever add a resource, never remove one.
    """
    result = await db.execute(
        select(TagGroup)
        .where(TagGroup.user_id == user_id)
        .options(selectinload(TagGroup.members))
    )
    groups = result.scalars().all()

    if not groups:
        return tags, [], {}

    # A group is identified by its tag as the user embedded it. The parsed
    # name alone is not enough: parse_tag strips TAG_TYPES prefixes, so a
    # group tagged "taggroup-kok" or "kok-lore-set" yields a parsed name
    # ("kok" / "set") that never equals the stored tag. Parsed names count
    # only for taggroup/unknown tags (the un-typed namespace); the RAW
    # string counts for every tag, because it is verbatim what the copy
    # button put inside <#...#> -- even when it parsed as a resource-type
    # tag ("kok-lore-set" parses as lore). Union semantics: an embedded tag
    # that happens to match both a resource and a group activates both.
    group_tag_refs: set[str] = set()
    for t in tags:
        if t.get("type") in ("taggroup", "unknown"):
            name = t.get("name")
            if name:
                group_tag_refs.add(name)
        raw = t.get("raw")
        if raw:
            group_tag_refs.add(raw)

    activated_groups: list[TagGroup] = []
    for group in groups:
        if group.is_active:
            activated_groups.append(group)
        elif group.tag and group.tag in group_tag_refs:
            activated_groups.append(group)

    if not activated_groups:
        return tags, [], {}

    direct_member_ids: dict[str, set[str]] = {}
    for group in activated_groups:
        if not group.activate_members:
            continue
        for member in group.members:
            direct_member_ids.setdefault(member.member_type, set()).add(member.member_id)

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

    return expanded, activated_group_names, direct_member_ids


def group_direct_members(body_json: dict[str, Any] | None) -> set[str]:
    """Resource ids activated directly by opted-in tag groups this request
    (the seventh activation source, rule 19). Reads the
    ``_gitv_group_member_ids`` body key the proxy stores from
    resolve_group_tags; every decision site ORs the result in beside
    should_activate_resource, so the source only ever adds.

    A direct id for a resource that no longer exists matches nothing --
    loaders only ever OR ids onto rows they already loaded.
    """
    ids = (body_json or {}).get("_gitv_group_member_ids") or {}
    out: set[str] = set()
    for members in ids.values():
        out |= {m for m in (members or ())}
    return out
