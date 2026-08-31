"""Moving a Map between installs: export serialization and import building.

Shared by the Maps import/export endpoints and the content-pack installer, so a
map published to a pack installs into exactly the same shape as one imported by
hand. Both used to live in the routers, where the pack code could not reach
them: installing a map from a pack created nothing at all, and publishing one
wrote a lossy format that could not be imported back.

A map is a *collection of objects*, not a blob. Each stage resource is resolved
on its own -- deduplicated against what the user already has, scanned, and given
its own provenance record -- so installing a dozen maps that share one dice
cantrip yields one dice cantrip, not a dozen.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.linked_repo import InstalledItem, LinkedRepo
from app.models.map import Map, MapStage, MapStageResource
from app.services.resource_identity import (
    content_hash,
    join_resource_path,
    normalize_repo_url,
    parse_resource_path,
)
from app.services.safety_scanner import (
    EmbeddedResource,
    ScanResult,
    decompose_map,
    scan_embedded_resource,
)

logger = logging.getLogger(__name__)

VALID_RESOURCE_MODES = ("smart", "keep_both", "reuse", "overwrite")
DEFAULT_RESOURCE_MODE = "smart"

EXPORT_MODES = ("embedded", "linked")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class ImportContext:
    """Everything resolution needs beyond the map JSON itself."""

    user_id: str
    resource_mode: str = DEFAULT_RESOURCE_MODE
    is_active: bool = True
    # The repo the map came from, when installed through a pack. Gives meaning
    # to a source with an empty url ("same repo as me") and lets linked
    # resources be fetched.
    repo: LinkedRepo | None = None
    # Repo-relative path of the map file, e.g. "maps/pipeline.json". Embedded
    # resources derive their origin from it as "<map_path>:<name>", so
    # re-installing the same map matches by origin instead of by hash.
    map_path: str = ""
    # Manual JSON import has no repo context and must never reach the network.
    allow_fetch: bool = True
    # Content read during _prefetch_linked, keyed by repo-relative file path.
    fetched: dict[str, str] = field(default_factory=dict)

    @property
    def repo_url(self) -> str:
        return normalize_repo_url(self.repo.url) if self.repo else ""


@dataclass
class ResolvedResource:
    """What happened to one object carried by the map."""

    resource: EmbeddedResource
    resource_id: str = ""
    # reused_origin | reused_hash | reused_name | created | updated | unresolved
    action: str = "unresolved"
    origin_url: str = ""
    origin_path: str = ""
    content_hash: str = ""
    scan: ScanResult | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def reused(self) -> bool:
        return self.action.startswith("reused")


@dataclass
class MapImportResult:
    map_obj: Map
    resources: list[ResolvedResource] = field(default_factory=list)
    # Normalized repo URLs a linked resource needed but that the user has not
    # linked. Never fetched implicitly -- cloning a repo the user never added is
    # a supply-chain hole, so we report and let them decide.
    requires_repos: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> list[ResolvedResource]:
        return [r for r in self.resources if not r.resource_id]


def _import_parameters(raw: Any, stage_name: str) -> str:
    """Validate a pack's stage parameters before they are stored.

    A map pack is untrusted input from another user's machine, so its parameter
    list goes through the same validator the API uses rather than being trusted
    as written -- this is the path by which a reserved name like `stream`, or an
    oversized blob, would otherwise reach the database. A list that does not
    validate is dropped with a warning rather than failing the whole install:
    losing a stage's tuning is recoverable, losing the map is not.

    Pre-0.24 packs have no parameters at all, which is the `None` case.
    """
    from app.services.llm_params import dump_params, validate_params

    if not raw:
        return "[]"
    try:
        return dump_params(validate_params(raw, f"stage '{stage_name}' parameters"))
    except Exception as exc:
        logger.warning(
            "Map import: dropping invalid parameters on stage '%s': %s", stage_name, exc
        )
        return "[]"


def _export_parameters(raw: str) -> list[dict[str, Any]]:
    """Stage parameters as pack JSON, read tolerantly so one corrupt blob cannot
    fail an entire export."""
    from app.services.llm_params import dump_params, parse_params

    return json.loads(dump_params(parse_params(raw)))


async def build_map_from_export(
    db: AsyncSession,
    user_id: str,
    data: dict[str, Any],
    name: str | None = None,
    description: str | None = None,
    resource_mode: str = DEFAULT_RESOURCE_MODE,
    is_active: bool = True,
    repo: LinkedRepo | None = None,
    map_path: str = "",
    allow_fetch: bool = True,
) -> MapImportResult:
    """Create a Map (and its stages and resources) from export JSON.

    ``resource_mode`` controls how a resource that collides with something the
    user already has is handled:

    - ``smart`` (default): reuse by origin, then by content hash, else create
    - ``keep_both``: always create new copies
    - ``reuse``: link to an existing same-named resource
    - ``overwrite``: update an existing same-named resource in place

    The caller commits. ``is_active=False`` is used by the pack installer, which
    installs everything disabled so an install cannot change behaviour until the
    user turns it on.
    """
    ctx = ImportContext(
        user_id=user_id,
        resource_mode=(
            resource_mode if resource_mode in VALID_RESOURCE_MODES else DEFAULT_RESOURCE_MODE
        ),
        is_active=is_active,
        repo=repo,
        map_path=map_path,
        allow_fetch=allow_fetch,
    )

    m = Map(
        user_id=user_id,
        name=name or data.get("name", "Imported Map"),
        description=description or data.get("description", ""),
        tag="",
        is_public=False,
        is_active=is_active,
        version=data.get("version", "1.0"),
        author=data.get("author", ""),
        global_llm_instructions=data.get("global_llm_instructions", ""),
    )
    db.add(m)
    await db.flush()

    result = MapImportResult(map_obj=m)

    # Decomposed up front so the scanner and the installer see one identical set
    # of objects; if they could disagree, something could install unscanned.
    embedded = decompose_map(data)
    by_position = {(r.stage_index, r.index): r for r in embedded}

    await _prefetch_linked(db, ctx, embedded, result)

    for stage_index, stage_data in enumerate(data.get("stages", []) or []):
        stage = MapStage(
            map_id=m.id,
            stage_order=stage_data.get("stage_order", 1),
            name=stage_data.get("name", "Stage"),
            description=stage_data.get("description", ""),
            system_instructions=stage_data.get("system_instructions", ""),
            endpoint_tag=stage_data.get("endpoint_tag", ""),
            model_override=stage_data.get("model_override", ""),
            driver_callable_turns=stage_data.get("driver_callable_turns", 0),
            verification_enabled=stage_data.get("verification_enabled", False),
            verification_model=stage_data.get("verification_model", ""),
            verification_max_retries=stage_data.get("verification_max_retries", 2),
            verification_instructions=stage_data.get("verification_instructions", ""),
            output_mode=stage_data.get("output_mode", "persist"),
            # A pack is untrusted input, so its parameters are re-validated
            # rather than stored as given -- this is where a `stream` or
            # `_gitv` key would otherwise enter. Absent on pre-0.24 packs.
            parameters_json=_import_parameters(
                stage_data.get("parameters"), stage_data.get("name", "Stage")
            ),
            verification_parameters_json=_import_parameters(
                stage_data.get("verification_parameters"), stage_data.get("name", "Stage")
            ),
        )
        db.add(stage)
        await db.flush()

        for idx, res_data in enumerate(stage_data.get("resources", []) or []):
            key = (stage_index, idx)
            res = by_position.get(key)
            if res is None:
                continue

            resolved = await _resolve_resource(db, ctx, res)
            result.resources.append(resolved)

            if resolved.origin_url and not resolved.resource_id:
                if resolved.origin_url not in result.requires_repos:
                    result.requires_repos.append(resolved.origin_url)

            if not resolved.resource_id:
                logger.warning(
                    "Map import: could not resolve %s", res.label
                )
                continue

            db.add(MapStageResource(
                map_stage_id=stage.id,
                resource_type=res.resource_type,
                resource_id=resolved.resource_id,
                position=res_data.get("position", "pre_driver"),
                sticky=res_data.get("sticky", False),
            ))

    for resolved in result.resources:
        result.warnings.extend(resolved.warnings)

    return result


async def _prefetch_linked(
    db: AsyncSession,
    ctx: ImportContext,
    embedded: list[EmbeddedResource],
    result: MapImportResult,
) -> None:
    """Fetch every linked resource that is not already installed, in one clone.

    Cloning per file made a map with five linked resources five full clones.
    Resources already satisfiable from what the user has are skipped entirely,
    so the common re-install case touches the network not at all.
    """
    wanted: dict[str, list[str]] = {}

    for res in embedded:
        if not res.is_linked:
            continue
        origin_url, origin_path = _origin_for(ctx, res)
        if not origin_path:
            continue
        if await _find_by_origin(db, ctx.user_id, origin_url, origin_path, res.resource_type):
            continue
        file_path, _key = parse_resource_path(origin_path)
        wanted.setdefault(origin_url, []).append(file_path)

    if not wanted:
        return

    for origin_url, paths in wanted.items():
        repo = await _find_repo(db, ctx.user_id, origin_url)
        if repo is None or not ctx.allow_fetch:
            if origin_url and origin_url not in result.requires_repos:
                result.requires_repos.append(origin_url)
            continue
        try:
            ctx.fetched.update(_read_repo_files(repo, sorted(set(paths))))
        except Exception:
            logger.exception("Map import: failed reading %s from %s", paths, origin_url)
            if origin_url not in result.requires_repos:
                result.requires_repos.append(origin_url)


def _read_repo_files(repo: LinkedRepo, paths: list[str]) -> dict[str, str]:
    from app.services.git_sync import (
        UnsafeRepoPathError,
        fetch_files_content,
        safe_repo_join,
    )

    if repo.is_local:
        out: dict[str, str] = {}
        for path in paths:
            try:
                full = safe_repo_join(repo.url, path)
            except UnsafeRepoPathError:
                logger.warning("Map import: refusing unsafe path %s", path)
                continue
            if full.is_file():
                out[path] = full.read_text(encoding="utf-8")
        return out

    return fetch_files_content(repo.url, paths, repo.token)


def _origin_for(ctx: ImportContext, res: EmbeddedResource) -> tuple[str, str]:
    """The (normalized url, path) this resource is identified by.

    An explicit ``source`` wins. Failing that, a resource embedded in a map that
    itself came from a repo is identified through that map --
    ``maps/pipeline.json:Dice Controller`` -- which is what lets a re-install of
    the same map match by origin rather than falling back to hashing.
    """
    if res.source_path:
        url = normalize_repo_url(res.source_url) or ctx.repo_url
        return url, res.source_path

    if ctx.map_path and ctx.repo_url:
        return ctx.repo_url, join_resource_path(ctx.map_path, res.name)

    return "", ""


async def _resolve_resource(
    db: AsyncSession, ctx: ImportContext, res: EmbeddedResource
) -> ResolvedResource:
    origin_url, origin_path = _origin_for(ctx, res)
    resolved = ResolvedResource(
        resource=res, origin_url=origin_url, origin_path=origin_path
    )

    content = res.content
    if content is not None:
        resolved.content_hash = content_hash(res.resource_type, content)

    smart = ctx.resource_mode == "smart"

    # 1. Same origin. Reuse without looking at the payload -- see below.
    if smart and origin_path:
        item = await _find_by_origin(
            db, ctx.user_id, origin_url, origin_path, res.resource_type
        )
        if item and item.local_id:
            resolved.resource_id = item.local_id
            resolved.action = "reused_origin"
            _warn_on_drift(resolved, item)
            return resolved

    # 2. Same content, wherever it came from. Catches hand-made copies and ones
    #    imported from a file, which have no origin at all.
    if smart and resolved.content_hash:
        existing_id = await _find_by_hash(
            db, ctx.user_id, res.resource_type, resolved.content_hash
        )
        if existing_id:
            resolved.resource_id = existing_id
            resolved.action = "reused_hash"
            return resolved

    # 3. Linked but not yet installed: resolve from the repo.
    if content is None:
        content = _content_from_fetch(ctx, res, origin_path)
        if content is None:
            resolved.action = "unresolved"
            resolved.warnings.append(
                f"{res.label} is linked to '{origin_path or 'an unknown path'}' "
                "but could not be resolved; link the source repo and re-import."
            )
            return resolved
        resolved.content_hash = content_hash(res.resource_type, content)
        # A fetched payload is code the user has not seen. Scan it exactly as a
        # direct install would.
        resolved.scan = scan_embedded_resource(
            EmbeddedResource(
                resource_type=res.resource_type, name=res.name, content=content
            )
        )
        # Now that we have content, the hash may match something already held.
        if smart:
            existing_id = await _find_by_hash(
                db, ctx.user_id, res.resource_type, resolved.content_hash
            )
            if existing_id:
                resolved.resource_id = existing_id
                resolved.action = "reused_hash"
                return resolved
    else:
        resolved.scan = scan_embedded_resource(res)

    # 4. Legacy name-based modes, then create.
    if ctx.resource_mode in ("reuse", "overwrite"):
        existing = await _find_by_name(
            db, ctx.user_id, res.resource_type, content.get("name", "")
        )
        if existing is not None:
            if ctx.resource_mode == "reuse":
                resolved.resource_id = _row_id(existing)
                resolved.action = "reused_name"
                return resolved
            await _update_resource(db, res.resource_type, existing, content)
            resolved.resource_id = _row_id(existing)
            resolved.action = "updated"
            return resolved

    resolved.resource_id = await _create_resource(
        db, ctx.user_id, res.resource_type, content, ctx.is_active
    )
    resolved.action = "created"
    return resolved


def _content_from_fetch(
    ctx: ImportContext, res: EmbeddedResource, origin_path: str
) -> dict | None:
    """Pull a linked resource out of what _prefetch_linked read."""
    if not origin_path:
        return None
    file_path, key = parse_resource_path(origin_path)
    raw = ctx.fetched.get(file_path)
    if raw is None:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Map import: %s is not valid JSON", file_path)
        return None

    if not key:
        return data if isinstance(data, dict) else None

    # A composite path addresses a resource *inside* a file -- a cantrip that a
    # pack ships only as part of a map.
    for candidate in decompose_map(data):
        if candidate.name == key and candidate.resource_type == res.resource_type:
            return candidate.content
    logger.warning("Map import: '%s' not found inside %s", key, file_path)
    return None


def _warn_on_drift(resolved: ResolvedResource, item: InstalledItem) -> None:
    """Flag a map whose payload disagrees with the copy we already trust.

    We reuse the installed copy and never overwrite it. A map must not be able
    to swap out code the user has already vetted by claiming a trusted path, so
    a mismatch is reported rather than applied.
    """
    declared = resolved.resource.declared_hash or resolved.content_hash
    if not declared or not item.content_hash:
        return
    if declared == item.content_hash:
        return
    resolved.warnings.append(
        f"{resolved.resource.label} ships a different version than your installed "
        f"copy from '{item.file_path}'. Your copy was kept unchanged."
    )


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


async def _find_by_origin(
    db: AsyncSession, user_id: str, origin_url: str, origin_path: str, res_type: str
) -> InstalledItem | None:
    if not origin_path:
        return None
    types = ("skill", "sample") if res_type in ("skill", "sample") else (res_type,)
    result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.user_id == user_id,
            InstalledItem.source_url == origin_url,
            InstalledItem.file_path == origin_path,
            InstalledItem.type.in_(types),
        )
    )
    for item in result.scalars().all():
        if item.local_id and await _resource_exists(db, item.type, item.local_id):
            return item
    return None


async def _find_by_hash(
    db: AsyncSession, user_id: str, res_type: str, digest: str
) -> str | None:
    """Match a hash against installed items, then against everything the user has.

    Installed items carry a stored hash; resources created by hand or through a
    file import do not, so they are hashed on demand. Any hash computed here is
    written back to the installed-item row it belongs to, which is the lazy
    backfill migration 043 could not do in SQL.
    """
    types = ("skill", "sample") if res_type in ("skill", "sample") else (res_type,)

    result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.user_id == user_id,
            InstalledItem.type.in_(types),
        )
    )
    items = list(result.scalars().all())

    for item in items:
        if item.content_hash == digest and item.local_id:
            if await _resource_exists(db, item.type, item.local_id):
                return item.local_id

    # Backfill empty hashes, then re-check.
    for item in items:
        if item.content_hash or not item.local_id:
            continue
        content = await _content_of(db, item.type, item.local_id)
        if content is None:
            continue
        item.content_hash = content_hash(item.type, content)
        if item.content_hash == digest:
            return item.local_id

    for row in await _all_resources(db, user_id, res_type):
        content = _content_of_row(res_type, row)
        if content_hash(res_type, content) == digest:
            return _row_id(row)

    return None


async def _find_by_name(db: AsyncSession, user_id: str, res_type: str, name: str):
    if not name:
        return None
    model = _model_for(res_type)
    if model is None:
        return None
    result = await db.execute(
        select(model).where(model.user_id == user_id, model.name == name)
    )
    return result.scalars().first()


def _model_for(res_type: str):
    if res_type == "cantrip":
        from app.models.cantrip import Cantrip
        return Cantrip
    if res_type == "lorebook":
        from app.models.lorebook import Lorebook
        return Lorebook
    if res_type in ("skill", "sample"):
        from app.models.skill import Skill
        return Skill
    return None


def _row_id(row) -> str:
    return row.id


async def _resource_exists(db: AsyncSession, res_type: str, resource_id: str) -> bool:
    model = _model_for(res_type)
    if model is None:
        return False
    result = await db.execute(select(model.id).where(model.id == resource_id))
    return result.scalar_one_or_none() is not None


async def _all_resources(db: AsyncSession, user_id: str, res_type: str) -> list:
    model = _model_for(res_type)
    if model is None:
        return []
    if res_type == "lorebook":
        result = await db.execute(
            select(model)
            .where(model.user_id == user_id)
            .options(selectinload(model.entries))
        )
    else:
        result = await db.execute(select(model).where(model.user_id == user_id))
    return list(result.scalars().all())


async def _content_of(db: AsyncSession, res_type: str, resource_id: str) -> dict | None:
    model = _model_for(res_type)
    if model is None:
        return None
    if res_type == "lorebook":
        result = await db.execute(
            select(model).where(model.id == resource_id).options(selectinload(model.entries))
        )
    else:
        result = await db.execute(select(model).where(model.id == resource_id))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return _content_of_row(res_type, row)


def _content_of_row(res_type: str, row) -> dict:
    """A stored row rendered back into export-shaped content, for hashing."""
    if res_type == "cantrip":
        return {
            "name": row.name,
            "description": row.description,
            "code": row.code,
            "llm_instructions": row.llm_instructions,
            "timeout_ms": row.timeout_ms,
            "execution_order": row.execution_order,
            "run_driver_callable": row.run_driver_callable,
        }
    if res_type == "lorebook":
        return {
            "name": row.name,
            "description": row.description,
            "entries": [
                {
                    "name": e.name,
                    "keys": json.loads(e.keys) if e.keys else [],
                    "secondary_keys": json.loads(e.secondary_keys) if e.secondary_keys else [],
                    "content": e.content,
                    "position": e.position,
                    "insertion_order": e.insertion_order,
                    "is_constant": e.is_constant,
                    "is_selective": e.is_selective,
                }
                for e in row.entries
            ],
        }
    if res_type in ("skill", "sample"):
        return {
            "name": row.name,
            "description": row.description,
            "content": row.content,
            "type": row.type,
        }
    return {"name": getattr(row, "name", "")}


# ---------------------------------------------------------------------------
# Creation / update
# ---------------------------------------------------------------------------


async def _create_resource(
    db: AsyncSession, user_id: str, res_type: str, content: dict, is_active: bool
) -> str:
    if res_type == "lorebook":
        from app.models.lorebook import Lorebook
        from app.models.lorebook_entry import LorebookEntry

        lb = Lorebook(
            user_id=user_id,
            name=content.get("name", "Imported Lorebook"),
            description=content.get("description", ""),
            is_active=is_active,
        )
        db.add(lb)
        await db.flush()
        for entry_data in content.get("entries", []) or []:
            db.add(_build_entry(lb.id, entry_data, LorebookEntry))
        return lb.id

    if res_type == "cantrip":
        from app.models.cantrip import Cantrip

        c = Cantrip(
            user_id=user_id,
            name=content.get("name", "Imported Cantrip"),
            description=content.get("description", ""),
            llm_instructions=content.get("llm_instructions", ""),
            code=content.get("code", ""),
            timeout_ms=content.get("timeout_ms", 5000),
            execution_order=content.get("execution_order", 10),
            is_active=is_active,
            run_driver_callable=content.get("run_driver_callable", True),
        )
        db.add(c)
        await db.flush()
        return c.id

    if res_type in ("skill", "sample"):
        from app.models.skill import Skill

        declared = content.get("type")
        s = Skill(
            user_id=user_id,
            name=content.get("name", "Imported Skill"),
            description=content.get("description", ""),
            content=content.get("content", ""),
            type=declared if declared in ("skill", "sample") else res_type,
        )
        db.add(s)
        await db.flush()
        return s.id

    return ""


def _build_entry(lorebook_id: str, entry_data: dict, entry_model):
    return entry_model(
        lorebook_id=lorebook_id,
        name=entry_data.get("name", ""),
        keys=json.dumps(entry_data.get("keys", [])),
        secondary_keys=json.dumps(
            entry_data.get("secondary_keys", entry_data.get("secondary_key", []))
        ),
        content=entry_data.get("content", ""),
        content_summary=entry_data.get("content_summary", ""),
        content_bullets=entry_data.get("content_bullets", ""),
        position=entry_data.get("position", "before_last_message"),
        insertion_order=entry_data.get("insertion_order", 10),
        is_constant=entry_data.get("is_constant", False),
        is_selective=entry_data.get("is_selective", False),
        is_disabled=entry_data.get("is_disabled", False),
        character_limit=entry_data.get("character_limit", 0),
    )


async def _update_resource(db: AsyncSession, res_type: str, row, content: dict) -> None:
    if res_type == "lorebook":
        from sqlalchemy import delete as sa_delete

        from app.models.lorebook_entry import LorebookEntry

        row.description = content.get("description", "")
        await db.flush()
        await db.execute(
            sa_delete(LorebookEntry).where(LorebookEntry.lorebook_id == row.id)
        )
        for entry_data in content.get("entries", []) or []:
            db.add(_build_entry(row.id, entry_data, LorebookEntry))
        await db.flush()
        return

    if res_type == "cantrip":
        row.description = content.get("description", "")
        row.llm_instructions = content.get("llm_instructions", "")
        row.code = content.get("code", "")
        row.timeout_ms = content.get("timeout_ms", row.timeout_ms)
        row.execution_order = content.get("execution_order", row.execution_order)
        await db.flush()
        return

    if res_type in ("skill", "sample"):
        declared = content.get("type")
        row.description = content.get("description", "")
        row.content = content.get("content", "")
        row.type = declared if declared in ("skill", "sample") else res_type
        await db.flush()


async def _find_repo(
    db: AsyncSession, user_id: str, origin_url: str
) -> LinkedRepo | None:
    """A repo the user has linked whose URL normalizes to origin_url.

    Only the user's own repos and ones marked global are considered, matching
    _resolve_repo_access. A repo the user has not linked is never cloned on the
    strength of a path claimed inside a map.
    """
    if not origin_url:
        return None
    result = await db.execute(select(LinkedRepo))
    for repo in result.scalars().all():
        if repo.user_id != user_id and not repo.is_global:
            continue
        if normalize_repo_url(repo.url) == origin_url:
            return repo
    return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def serialize_map_to_export(
    db: AsyncSession, m: Map, mode: str = "embedded"
) -> dict[str, Any]:
    """Serialize a Map to the self-contained export format.

    ``embedded`` (default) writes the full content of every attached resource,
    so the result installs on a machine with no repos linked. ``linked`` writes
    only a ``source`` for any resource whose origin is known, so the map tracks
    upstream and the author maintains each cantrip in exactly one place.

    Either way a ``source`` block is emitted when the origin is known, which is
    what lets the importer deduplicate. ``endpoint_id`` is deliberately omitted
    -- a local UUID that means nothing elsewhere -- in favour of
    ``endpoint_tag``, which does.

    The map must be loaded with its stages and stage resources eagerly, e.g.
    ``.options(selectinload(Map.stages).selectinload(MapStage.resources))``.
    """
    mode = mode if mode in EXPORT_MODES else "embedded"

    export_data: dict[str, Any] = {
        "name": m.name,
        "description": m.description,
        "version": m.version,
        "author": m.author,
        "global_llm_instructions": m.global_llm_instructions,
        "stages": [],
    }

    origins = await _origins_for_user(db, m.user_id)

    for stage in sorted(m.stages, key=lambda s: s.stage_order):
        stage_data: dict[str, Any] = {
            "stage_order": stage.stage_order,
            "name": stage.name,
            "description": stage.description,
            "system_instructions": stage.system_instructions,
            "endpoint_tag": stage.endpoint_tag,
            "model_override": stage.model_override,
            "driver_callable_turns": stage.driver_callable_turns,
            "verification_enabled": stage.verification_enabled,
            "verification_model": stage.verification_model,
            "verification_max_retries": stage.verification_max_retries,
            "verification_instructions": stage.verification_instructions,
            "output_mode": stage.output_mode,
            "parameters": _export_parameters(stage.parameters_json),
            "verification_parameters": _export_parameters(stage.verification_parameters_json),
            "resources": [],
        }

        for res in stage.resources:
            res_data: dict[str, Any] = {
                "resource_type": res.resource_type,
                "position": res.position,
                "sticky": res.sticky,
            }

            content = await _content_of(db, res.resource_type, res.resource_id)
            if content is None:
                continue

            name = content.get("name", "")
            res_data["resource_name"] = name

            item = origins.get(res.resource_id)
            digest = content_hash(res.resource_type, content)
            if item is not None:
                res_data["source"] = {
                    "url": item.source_url,
                    "path": item.file_path,
                    "version": item.installed_version,
                    "content_hash": item.content_hash or digest,
                }

            # Linked mode drops the payload only where a source can replace it.
            if mode == "linked" and "source" in res_data:
                stage_data["resources"].append(res_data)
                continue

            res_data["resource_content"] = _export_content(res.resource_type, content)
            stage_data["resources"].append(res_data)

        export_data["stages"].append(stage_data)

    return export_data


def _export_content(res_type: str, content: dict) -> dict:
    """Content in the shape the importer expects, with descriptions restored."""
    if res_type == "lorebook":
        return {
            "name": content.get("name", ""),
            "description": content.get("description", ""),
            "entries": content.get("entries", []),
        }
    if res_type == "cantrip":
        return {
            "name": content.get("name", ""),
            "description": content.get("description", ""),
            "llm_instructions": content.get("llm_instructions", ""),
            "code": content.get("code", ""),
            "timeout_ms": content.get("timeout_ms", 5000),
            "execution_order": content.get("execution_order", 10),
            "run_driver_callable": content.get("run_driver_callable", True),
        }
    return {
        "name": content.get("name", ""),
        "description": content.get("description", ""),
        "content": content.get("content", ""),
        "type": content.get("type", res_type),
    }


async def _origins_for_user(
    db: AsyncSession, user_id: str
) -> dict[str, InstalledItem]:
    """local_id -> the installed-item row that records where it came from."""
    result = await db.execute(
        select(InstalledItem).where(
            InstalledItem.user_id == user_id,
            InstalledItem.local_id.is_not(None),
        )
    )
    out: dict[str, InstalledItem] = {}
    for item in result.scalars().all():
        if item.local_id and item.file_path:
            out.setdefault(item.local_id, item)
    return out
