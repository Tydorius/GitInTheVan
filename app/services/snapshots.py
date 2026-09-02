"""Per-object version history (Phase 25).

Every update and delete of a user-authored resource takes a ``pre_edit``
snapshot first, inside the router that performs it. Doing it there rather than
in any one caller is the whole point: the Assistant Pane executes its tools
*through* these same routers, so an assistant write gets an undo with no
assistant-specific code, and so does the user's own edit in the UI.

Two deliberate choices worth knowing before changing anything here.

**The serializer is column-introspective, not a field list.** Every existing
"resource to dict" function in this codebase -- ``_content_of_row`` and
``serialize_map_to_export`` in ``map_transfer.py``, ``_serialize_resource`` in
``routers/packs.py`` -- is *export*-shaped and lossy: they drop ``tag``,
``is_active``, ``is_public``, ``budget_weight`` and the ``run_*`` routing flags,
because a published resource does not carry a local install's wiring. A snapshot
that dropped those would restore an object that looks right and behaves
differently. Reading ``__table__.columns`` instead means a column added in a
later phase is captured without anyone remembering to add it here -- the 0.18.0
``skills.budget_weight`` class of bug, in a new place.

**The dedup hash is over the snapshot JSON, not**
``resource_identity.content_hash``. That function deliberately ignores
description, tag and the activation flags, because re-tagging a cantrip is not a
new cantrip for *deduplication* purposes. For version history it is exactly a new
version, so identity hashing is the wrong question to ask here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.admin_settings import AdminSettings
from app.models.cantrip import Cantrip
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.models.memory_rule import MemoryRule
from app.models.resource_snapshot import ResourceSnapshot
from app.models.scenario_rule import ScenarioRule
from app.models.skill import Skill
from app.models.verification import VerificationRule
from app.services.content_guard import check_size, log_scan_findings, sanitize_and_log

logger = logging.getLogger(__name__)

SOURCE_PRE_EDIT = "pre_edit"
SOURCE_MANUAL = "manual"

# Identity, ownership and bookkeeping. Everything else on the model is content a
# restore has to put back.
_SKIP_COLUMNS = frozenset({"id", "user_id", "created_at", "updated_at"})
_SKIP_CHILD_COLUMNS = frozenset({"id", "created_at"})


@dataclass(frozen=True)
class SnapshotSpec:
    """How one resource type is captured, guarded and restored.

    ``scan_type`` is the vocabulary ``safety_scanner.scan_json_content`` speaks,
    which predates this module and is not the same set of strings as
    ``resource_type`` -- a verification rule is ``rule`` there. An empty
    ``scan_type`` means the scanner has no rules for this shape; the size check
    and the sanitizer still run.
    """

    resource_type: str
    model: Any
    label: str
    scan_type: str = ""
    # Column on AdminSettings holding this type's size limit, in KB.
    size_cap_attr: str = ""
    # Fields the size limit applies to.
    size_fields: tuple[str, ...] = ()
    # Fields the sanitizer applies to. Cantrip *code* is deliberately absent:
    # strip_control_chars is safe for prose but can corrupt a string literal in
    # executable JavaScript. See Planning/security-control-document.md.
    sanitize_fields: tuple[str, ...] = ()
    # Relationship attribute holding child rows, and the FK back to the parent.
    child_attr: str = ""
    child_model: Any = None
    child_fk: str = ""
    # Child fields the sanitizer applies to; the size cap is their sum.
    child_sanitize_fields: tuple[str, ...] = ()
    # Whether `tag` is unique per user for this type. Restore-as-new clears it.
    has_tag: bool = True


SNAPSHOT_TYPES: dict[str, SnapshotSpec] = {
    "cantrip": SnapshotSpec(
        resource_type="cantrip",
        model=Cantrip,
        label="Cantrip code",
        scan_type="cantrip",
        size_cap_attr="max_script_size_kb",
        size_fields=("code",),
    ),
    "lorebook": SnapshotSpec(
        resource_type="lorebook",
        model=Lorebook,
        label="Lorebook content",
        scan_type="lorebook",
        size_cap_attr="max_lorebook_size_kb",
        child_attr="entries",
        child_model=LorebookEntry,
        child_fk="lorebook_id",
        child_sanitize_fields=("content",),
    ),
    "skill": SnapshotSpec(
        resource_type="skill",
        model=Skill,
        label="Skill content",
        size_cap_attr="max_rule_size_kb",
        size_fields=("content",),
        sanitize_fields=("content",),
        has_tag=False,
    ),
    "verification_rule": SnapshotSpec(
        resource_type="verification_rule",
        model=VerificationRule,
        label="Rule prompt",
        scan_type="rule",
        size_cap_attr="max_rule_size_kb",
        size_fields=("prompt",),
        sanitize_fields=("prompt",),
    ),
    "memory_rule": SnapshotSpec(
        resource_type="memory_rule",
        model=MemoryRule,
        label="Memory rule prompt",
        scan_type="rule",
        size_cap_attr="max_rule_size_kb",
        size_fields=("prompt",),
        sanitize_fields=("prompt",),
    ),
    "scenario_rule": SnapshotSpec(
        resource_type="scenario_rule",
        model=ScenarioRule,
        label="Scenario rule prompt",
        scan_type="rule",
        size_cap_attr="max_rule_size_kb",
        size_fields=("prompt",),
        sanitize_fields=("prompt",),
        # ScenarioRule fires on a token threshold, not a tag -- it has no tag column.
        has_tag=False,
    ),
}

# `sample` rows live on the Skill table and differ only by their `type` column.
# Callers may pass either; both resolve to the same spec, and the stored `type`
# decides what a restore recreates.
_TYPE_ALIASES = {"sample": "skill"}


class SnapshotError(Exception):
    """A snapshot operation could not proceed. Carries a user-facing message."""


def resolve_type(resource_type: str) -> str:
    """Canonical spec key for a caller-supplied type string, or an empty string."""
    key = (resource_type or "").strip()
    key = _TYPE_ALIASES.get(key, key)
    return key if key in SNAPSHOT_TYPES else ""


def spec_for(resource_type: str) -> SnapshotSpec:
    key = resolve_type(resource_type)
    if not key:
        raise SnapshotError(f"Snapshots are not kept for '{resource_type}'.")
    return SNAPSHOT_TYPES[key]


async def _admin_setting(db: AsyncSession, name: str, fallback: int) -> int:
    """Read one admin cap through the caller's own session.

    Deliberately not `get_admin_settings()`, which opens a session of its own.
    Everything in this module runs *between* a caller's write and its commit,
    and a nested session on SQLite shares one connection through StaticPool --
    so its close issues a ROLLBACK that discards the caller's pending work. That
    is exactly how the first version of this module lost every snapshot it took.
    """
    result = await db.execute(select(getattr(AdminSettings, name)))
    value = result.scalars().first()
    return int(value) if value is not None else fallback


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _columns_of(model: Any, skip: frozenset[str]) -> tuple[str, ...]:
    return tuple(c.key for c in model.__table__.columns if c.key not in skip)


def content_columns(spec: SnapshotSpec) -> tuple[str, ...]:
    return _columns_of(spec.model, _SKIP_COLUMNS)


def child_columns(spec: SnapshotSpec) -> tuple[str, ...]:
    if not spec.child_attr:
        return ()
    return _columns_of(spec.child_model, _SKIP_CHILD_COLUMNS | {spec.child_fk})


def serialize(spec: SnapshotSpec, row: Any) -> dict[str, Any]:
    """Every content column of a row, plus its children. Lossless by construction."""
    content: dict[str, Any] = {
        name: _json_safe(getattr(row, name)) for name in content_columns(spec)
    }
    if spec.child_attr:
        names = child_columns(spec)
        content[spec.child_attr] = [
            {name: _json_safe(getattr(child, name)) for name in names}
            for child in getattr(row, spec.child_attr)
        ]
    return content


def hash_content(content: dict[str, Any]) -> str:
    raw = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def load_row(db: AsyncSession, user_id: str, spec: SnapshotSpec, resource_id: str):
    """The live row, ownership-filtered, with children eager-loaded.

    Takes an id rather than a row on purpose: `update_lorebook` never
    eager-loads `entries`, so serializing the row the router already holds would
    lazy-load under async and raise.
    """
    query = select(spec.model).where(
        spec.model.id == resource_id, spec.model.user_id == user_id
    )
    if spec.child_attr:
        query = query.options(selectinload(getattr(spec.model, spec.child_attr)))
    result = await db.execute(query)
    return result.scalar_one_or_none()


async def capture(
    db: AsyncSession,
    user_id: str,
    resource_type: str,
    resource_id: str,
    *,
    source: str = SOURCE_PRE_EDIT,
    label: str = "",
    strict: bool = False,
) -> ResourceSnapshot | None:
    """Store the current state of one object. Adds; never flushes or commits.

    The caller's own trailing `db.commit()` lands it, and `get_db`'s session
    context manager rolls it back with everything else if the handler raises
    afterwards -- which is what stops a 409 on a tag collision from leaving an
    orphan snapshot behind.

    Not flushing is deliberate and load-bearing. Several of these handlers call
    `get_admin_settings()` between this call and their commit, which opens a
    session of its own; on SQLite that session shares one connection through
    StaticPool, so closing it ROLLBACKs whatever has been flushed but not
    committed. A flush here would therefore be discarded by the router's own
    size check a few lines later. The id is assigned in Python rather than at
    flush time so callers can still identify the row.

    Returns None when there is nothing to store: the object is gone, or its
    content is identical to the newest snapshot already held.

    A `pre_edit` capture is best-effort -- an unexpected failure is logged and
    swallowed rather than turned into a failed edit, because refusing to save a
    cantrip because history could not be written would be the worse outcome.
    `strict` inverts that, and the two callers that use it are the ones where
    silence would be dangerous: a manual save, which must not report success
    having stored nothing, and the pre-restore capture inside
    `restore_in_place`, which is the only copy of what the restore overwrites.
    """
    try:
        spec = spec_for(resource_type)
    except SnapshotError:
        if strict:
            raise
        logger.warning("No snapshot spec for resource type %s", resource_type)
        return None

    try:
        row = await load_row(db, user_id, spec, resource_id)
        if row is None:
            return None

        content = serialize(spec, row)
        digest = hash_content(content)

        latest = await db.execute(
            select(ResourceSnapshot.content_hash)
            .where(
                ResourceSnapshot.user_id == user_id,
                ResourceSnapshot.resource_type == spec.resource_type,
                ResourceSnapshot.resource_id == resource_id,
            )
            .order_by(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc())
            .limit(1)
        )
        if latest.scalar_one_or_none() == digest:
            return None

        # Prune before adding, so the new row is not counted against the cap it
        # is about to fall inside.
        await _prune(db, user_id, spec.resource_type, resource_id)

        snapshot = ResourceSnapshot(
            id=str(uuid.uuid4()),
            user_id=user_id,
            resource_type=spec.resource_type,
            resource_id=resource_id,
            resource_name=str(getattr(row, "name", ""))[:128],
            content_hash=digest,
            content_json=json.dumps(content, ensure_ascii=False),
            source=source if source in (SOURCE_PRE_EDIT, SOURCE_MANUAL) else SOURCE_PRE_EDIT,
            label=(label or "")[:128],
        )
        db.add(snapshot)
        return snapshot
    except Exception:
        if strict:
            raise
        logger.exception(
            "Snapshot capture failed for %s %s; the edit itself is unaffected",
            resource_type,
            resource_id,
        )
        return None


async def _prune(db: AsyncSession, user_id: str, resource_type: str, resource_id: str) -> None:
    """Trim automatic history to the admin's cap, oldest first.

    Called before the new row is added, and deletes without flushing, for the
    reason given on `capture`.

    Manual snapshots are pinned and never pruned -- the model
    `DebugExchange.saved` uses for debug runs. They still count towards the cap,
    so a user who pins twenty versions stops accumulating automatic ones rather
    than growing the table without limit.
    """
    cap = max(1, await _admin_setting(db, "max_snapshots_per_object", 20))

    result = await db.execute(
        select(ResourceSnapshot)
        .where(
            ResourceSnapshot.user_id == user_id,
            ResourceSnapshot.resource_type == resource_type,
            ResourceSnapshot.resource_id == resource_id,
        )
        .order_by(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc())
    )
    rows = list(result.scalars().all())
    # One is about to be added, so the cap leaves room for it.
    keep = max(0, cap - 1)
    for row in rows[keep:]:
        if row.source == SOURCE_MANUAL:
            continue
        await db.delete(row)


@dataclass
class RestoreReport:
    """What a restore actually did.

    `notes` carries anything the restore could not put back exactly. A
    degradation that still produces a working object has to say so -- a silent
    partial restore is worse than a refusal, because the user believes they are
    back on a known-good version.
    """

    resource_type: str
    resource_id: str
    name: str
    created: bool
    notes: list[str]


def load_content(snapshot: ResourceSnapshot) -> dict[str, Any]:
    try:
        content = json.loads(snapshot.content_json or "{}")
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"This snapshot's stored content is unreadable: {exc}") from exc
    if not isinstance(content, dict):
        raise SnapshotError("This snapshot's stored content is not an object.")
    return content


async def guard_content(
    db: AsyncSession,
    user_id: str,
    spec: SnapshotSpec,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Run the same checks the type's own create and update paths run.

    A stored snapshot is not trusted input just because this instance wrote it:
    the scanner's rules may have tightened since it was taken, and the admin's
    size limits may have come down. Returns the content with sanitized fields
    replaced, which is why a restore can differ by a control character from what
    was captured.

    Blocking behaviour matches create and update exactly: size is a hard 413,
    and the scanner logs its findings to the audit trail without blocking. A
    blocking override flow is separate, still-open work -- see
    Planning/security-control-document.md.
    """
    from app.services.safety_scanner import scan_json_content

    max_bytes = (
        await _admin_setting(db, spec.size_cap_attr, 0) * 1024 if spec.size_cap_attr else 0
    )

    guarded = dict(content)

    if max_bytes:
        for name in spec.size_fields:
            check_size(str(guarded.get(name) or ""), max_bytes, spec.label)

    for name in spec.sanitize_fields:
        value = guarded.get(name)
        if isinstance(value, str) and value:
            guarded[name] = await sanitize_and_log(db, user_id, value, spec.resource_type, "")

    if spec.child_attr:
        children = guarded.get(spec.child_attr) or []
        if not isinstance(children, list):
            raise SnapshotError(f"This snapshot's '{spec.child_attr}' is not a list.")
        # The lorebook cap is the total across entries, matching
        # _enforce_lorebook_size in routers/lorebook.py, not a per-entry cap.
        if max_bytes:
            total = sum(
                len(str(child.get(name) or ""))
                for child in children
                if isinstance(child, dict)
                for name in spec.child_sanitize_fields
            )
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"{spec.label} exceeds size limit ({total} chars, max {max_bytes})",
                )
        rebuilt = []
        for child in children:
            if not isinstance(child, dict):
                continue
            child = dict(child)
            for name in spec.child_sanitize_fields:
                value = child.get(name)
                if isinstance(value, str) and value:
                    child[name] = await sanitize_and_log(
                        db, user_id, value, f"{spec.resource_type}_entry", ""
                    )
            rebuilt.append(child)
        guarded[spec.child_attr] = rebuilt

    if spec.scan_type:
        scan = scan_json_content(json.dumps(guarded, ensure_ascii=False), spec.scan_type)
        await log_scan_findings(db, user_id, scan, spec.resource_type, "")

    return guarded


def _assignable(spec: SnapshotSpec, content: dict[str, Any]) -> dict[str, Any]:
    """Snapshot fields that map to a column on the model today.

    A snapshot taken before a column existed simply lacks it, and the model's own
    default applies. A snapshot carrying a column since removed is ignored rather
    than raising. Forward and backward, a restore degrades to the fields that
    still mean something.
    """
    known = set(content_columns(spec))
    return {k: v for k, v in content.items() if k in known}


async def _tag_taken(
    db: AsyncSession, spec: SnapshotSpec, user_id: str, tag: str, exclude_id: str
) -> bool:
    if not spec.has_tag or not tag:
        return False
    query = select(spec.model.id).where(spec.model.user_id == user_id, spec.model.tag == tag)
    if exclude_id:
        query = query.where(spec.model.id != exclude_id)
    result = await db.execute(query)
    return result.scalars().first() is not None


def _rebuild_children(db: AsyncSession, spec: SnapshotSpec, parent_id: str, content: dict) -> None:
    known = set(child_columns(spec))
    for child in content.get(spec.child_attr) or []:
        if not isinstance(child, dict):
            continue
        fields = {k: v for k, v in child.items() if k in known}
        db.add(spec.child_model(**{spec.child_fk: parent_id}, **fields))


async def restore_as_new(
    db: AsyncSession,
    user_id: str,
    snapshot: ResourceSnapshot,
    *,
    name: str = "",
) -> RestoreReport:
    """Create a copy of a stored version, leaving the live object untouched.

    The default, because 0.21.0's invariant governs here too: a match never
    overwrites. The copy's tag is cleared -- a tag is unique per user and is what
    activates a resource, so a copy carrying the original's tag would either
    collide outright or silently start firing in its place.
    """
    spec = spec_for(snapshot.resource_type)
    content = await guard_content(db, user_id, spec, load_content(snapshot))
    fields = _assignable(spec, content)
    notes: list[str] = []

    captured = str(fields.get("name") or snapshot.resource_name or "Restored")
    fields["name"] = (name.strip() or f"{captured} (restored)")[:128]

    if spec.has_tag and fields.get("tag"):
        notes.append(
            f"The tag '{fields['tag']}' was cleared on the copy: a tag activates one "
            "resource and has to stay unique."
        )
        fields["tag"] = ""

    row = spec.model(user_id=user_id, **fields)
    db.add(row)
    await db.flush()

    if spec.child_attr:
        _rebuild_children(db, spec, row.id, content)
        await db.flush()

    return RestoreReport(
        resource_type=spec.resource_type,
        resource_id=row.id,
        name=row.name,
        created=True,
        notes=notes,
    )


async def restore_in_place(
    db: AsyncSession,
    user_id: str,
    snapshot: ResourceSnapshot,
) -> RestoreReport:
    """Overwrite the live object with a stored version.

    Takes a `pre_edit` snapshot of the current state first, so the restore is
    itself undoable. Without that, a mis-clicked restore is exactly the
    unrecoverable write this phase exists to prevent.
    """
    spec = spec_for(snapshot.resource_type)
    row = await load_row(db, user_id, spec, snapshot.resource_id)
    if row is None:
        raise SnapshotError(
            "The object this snapshot came from no longer exists. "
            "Restore it as a new copy instead."
        )

    content = await guard_content(db, user_id, spec, load_content(snapshot))
    fields = _assignable(spec, content)
    notes: list[str] = []

    # strict: this is the only record of what the restore is about to overwrite.
    await capture(
        db, user_id, spec.resource_type, snapshot.resource_id,
        source=SOURCE_PRE_EDIT, strict=True,
    )

    if spec.has_tag and "tag" in fields:
        tag = str(fields.get("tag") or "")
        if tag and tag != row.tag and await _tag_taken(db, spec, user_id, tag, row.id):
            notes.append(
                f"The tag '{tag}' now belongs to another {spec.resource_type}, so this object "
                f"kept its current tag ('{row.tag}'). Everything else was restored."
            )
            fields.pop("tag")

    for key, value in fields.items():
        setattr(row, key, value)
    await db.flush()

    if spec.child_attr:
        from sqlalchemy import delete as sa_delete

        await db.execute(
            sa_delete(spec.child_model).where(getattr(spec.child_model, spec.child_fk) == row.id)
        )
        _rebuild_children(db, spec, row.id, content)
        await db.flush()

    return RestoreReport(
        resource_type=spec.resource_type,
        resource_id=row.id,
        name=row.name,
        created=False,
        notes=notes,
    )
