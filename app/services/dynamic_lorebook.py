"""Phase 20 — dynamic lorebooks: sandbox seeding and guarded op application.

``context.lorebook`` in the sandbox collects writes as an op log (the sandbox
has no host callbacks); this module is the host half:

- ``build_seed``: the read-side blob baked into the context — this
  conversation's script-created books (always, writable), the chat's lexicon
  book (read-only), and static books named in the cantrip's ``lorebook_reads``
  config (read-only, unless the name is a script-created cross-conversation
  book).
- ``apply_ops``: apply one cantrip run's ops to the real tables with the
  ``lorebook_writes`` guards (rule 18), in the lexicon order. Two phases on
  purpose: every guard in the batch runs before anything is constructed,
  because the guards open sessions of their own and a flushed-uncommitted row
  on the shared SQLite connection would be rolled back underneath us (the
  snapshots.capture invariant). One commit per batch.

Every refusal degrades: the op is dropped with an audit line and a debug
capture, and the request continues. A mid-request refusal must never fail the
reply (lexicon semantics). Sandbox and replay runs drop all ops.

Same-turn visibility: applied adds/updates come back as ``MatchedEntry`` rows
so the caller can inject them this turn (mechanism B); they also ride
``body_json['_gitv_dynamic_lorebook_delta']`` for the injection sites that run
after the cantrip pass returns.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.conversation_dynamic_lorebook import ConversationDynamicLorebook
from app.models.conversation_lorebook_binding import ConversationLorebookBinding
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.services.lorebook import MatchedEntry, parse_json_list
from app.services.lorebook_writes import (
    enforce_lorebook_size,
    sanitize_entry_content,
    scan_entry,
)

logger = logging.getLogger(__name__)

# Open question 1 default (refresh doc §5): a script can emit unbounded ops,
# so both the sandbox (per run) and the host (per request, authoritative --
# it spans every cantrip position and map stage) refuse beyond this many.
MAX_OPS_PER_REQUEST = 50

# Body keys (house style, like _gitv_metrics): the accumulated same-turn
# injection delta and the per-request op counter.
DELTA_KEY = "_gitv_dynamic_lorebook_delta"
OPS_USED_KEY = "_gitv_lorebook_ops_used"

_VALID_POSITIONS = ("system_start", "before_last_message")


def parse_reads(raw: str | None) -> list[str]:
    """Cantrip.lorebook_reads as a name list. The column is nullable on MySQL
    (no literal TEXT defaults), so NULL and any malformed legacy value both
    mean "none configured"."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        name = str(item).strip()
        if name and name not in out:
            out.append(name)
    return out[:20]


def _entry_seed(entry: LorebookEntry) -> dict[str, Any]:
    try:
        keys = parse_json_list(entry.keys or "[]")
    except Exception:  # noqa: BLE001 -- a corrupt legacy blob degrades to no keys
        keys = []
    return {
        "name": entry.name,
        "keys": keys,
        "content": entry.content,
        "position": entry.position,
        "insertion_order": entry.insertion_order,
        "is_constant": entry.is_constant,
        "is_selective": entry.is_selective,
    }


def _book_seed(book: Lorebook, writable: bool, cap_chars: int) -> tuple[dict[str, Any], bool]:
    """One book's seed blob, truncated to the cap from the end (the newest
    entries are the ones this turn's script most likely just wrote)."""
    entries = [e for e in book.entries if not e.is_disabled]
    truncated = False
    while entries and sum(len(e.content or "") for e in entries) > cap_chars:
        entries.pop()
        truncated = True
    seed = {"writable": writable, "entries": [_entry_seed(e) for e in entries]}
    return seed, truncated


async def build_seed(
    db: AsyncSession,
    body_json: dict[str, Any],
    user_id: str,
    conversation_id: str,
    extra_names: list[str] | None = None,
    capture: bool = True,
) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Read-side blob for ``context.__lorebook``.

    Always included: this conversation's script-created books (writable) and
    its lexicon book (read-only). ``extra_names`` adds books by name — the
    cantrip's ``lorebook_reads`` config — read-only unless the name belongs to
    a script-created cross-conversation book (conversation_id="" row), which
    is that book's own author writing to it.

    Returns ``(seed, always_names)`` so callers can subset per cantrip
    (always-bound books for everyone, read-config books per cantrip).
    """
    from app.services.admin import get_admin_settings
    from app.services.debug import debug_capture

    admin_settings = await get_admin_settings()
    cap_chars = admin_settings.max_lorebook_size_kb * 1024

    seed: dict[str, dict[str, Any]] = {}
    always: set[str] = set()
    notes: list[dict[str, Any]] = []

    # Script-created books bound to this conversation: always seeded, writable.
    dyn_rows = (
        await db.execute(
            select(ConversationDynamicLorebook)
            .where(
                ConversationDynamicLorebook.user_id == user_id,
                ConversationDynamicLorebook.conversation_id == conversation_id,
            )
            .order_by(ConversationDynamicLorebook.created_at)
        )
    ).scalars().all()
    if dyn_rows:
        books = (
            await db.execute(
                select(Lorebook)
                .where(
                    Lorebook.id.in_([r.lorebook_id for r in dyn_rows]),
                    Lorebook.user_id == user_id,
                )
                .options(selectinload(Lorebook.entries))
            )
        ).scalars().all()
        by_id = {b.id: b for b in books}
        for row in dyn_rows:
            book = by_id.get(row.lorebook_id)
            if book is None:
                continue
            blob, truncated = _book_seed(book, writable=True, cap_chars=cap_chars)
            seed[row.name] = blob
            always.add(row.name)
            notes.append({
                "book": row.name, "entries": len(blob["entries"]),
                "truncated": truncated, "writable": True,
            })

    # The chat's lexicon book (Phase 29): bound, so seeded too — read-only,
    # the lexicon compiler owns it.
    lex_binding = (
        await db.execute(
            select(ConversationLorebookBinding).where(
                ConversationLorebookBinding.user_id == user_id,
                ConversationLorebookBinding.conversation_id == conversation_id,
            )
        )
    ).scalar_one_or_none()
    if lex_binding is not None:
        book = (
            await db.execute(
                select(Lorebook)
                .where(
                    Lorebook.id == lex_binding.lorebook_id, Lorebook.user_id == user_id
                )
                .options(selectinload(Lorebook.entries))
            )
        ).scalar_one_or_none()
        if book is not None and book.name not in seed:
            blob, truncated = _book_seed(book, writable=False, cap_chars=cap_chars)
            seed[book.name] = blob
            always.add(book.name)
            notes.append({
                "book": book.name, "entries": len(blob["entries"]),
                "truncated": truncated, "writable": False,
            })

    # Named books (lorebook_reads): read-only unless script-created unbound.
    for name in extra_names or []:
        if name in seed:
            continue
        unbound = (
            await db.execute(
                select(ConversationDynamicLorebook).where(
                    ConversationDynamicLorebook.user_id == user_id,
                    ConversationDynamicLorebook.conversation_id == "",
                    ConversationDynamicLorebook.name == name,
                )
            )
        ).scalar_one_or_none()
        book = (
            (
                await db.execute(
                    select(Lorebook)
                    .where(Lorebook.user_id == user_id, Lorebook.name == name)
                    .order_by(Lorebook.created_at)
                    .options(selectinload(Lorebook.entries))
                )
            )
            .scalars()
            .first()
        )
        if book is None:
            notes.append({"book": name, "entries": 0, "missing": True})
            continue
        writable = unbound is not None
        blob, truncated = _book_seed(book, writable=writable, cap_chars=cap_chars)
        seed[name] = blob
        notes.append({
            "book": name, "entries": len(blob["entries"]),
            "truncated": truncated, "writable": writable,
        })

    # Captured only when there is something to say: users with no dynamic
    # feature in play would otherwise get a "No books seeded" stage on every
    # request. `capture=False` is the refresh path after ops applied (the
    # seeding capture has already run for this pass).
    if capture and (seed or extra_names):
        debug_capture(
            body_json, "lorebook_seeding", "Dynamic Lorebook Seeding",
            detail=(
                f"{len(seed)} book(s) seeded into the sandbox context"
                if seed else "No books found to seed"
            ),
            metadata={"books": notes},
        )
    return seed, always


def _coerce_fields(raw: Any, partial: bool) -> dict[str, Any] | None:
    """Op entry/fields payload -> column-shaped dict. None means malformed.

    ``partial`` keeps only the keys the op actually carries (update); a full
    add fills every column (missing values take the column defaults).
    """
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}

    def has(key: str) -> bool:
        return not partial or key in raw

    if has("content"):
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            if not partial:
                return None
        else:
            out["content"] = content
    if has("name"):
        name = raw.get("name")
        out["name"] = str(name)[:128] if name is not None else ""
    for key in ("keys", "secondary_keys"):
        if has(key):
            val = raw.get(key)
            items = [str(k).strip() for k in val] if isinstance(val, list) else []
            out[key] = json.dumps([k for k in items if k][:32])
    if has("position"):
        pos = str(raw.get("position") or "")
        # The two positions the inserter actually places; anything else would
        # fall into inject_entries' catch-all bucket.
        out["position"] = pos if pos in _VALID_POSITIONS else _VALID_POSITIONS[1]
    for key, default in (("insertion_order", 10), ("character_limit", 0)):
        if has(key):
            try:
                out[key] = int(raw.get(key))
            except (TypeError, ValueError):
                out[key] = default
    for key in ("is_constant", "is_selective"):
        if has(key):
            out[key] = raw.get(key) is True
    if not out and partial:
        return None
    return out


async def _find_dynamic_row(
    db: AsyncSession, user_id: str, conversation_id: str, name: str
) -> ConversationDynamicLorebook | None:
    return (
        await db.execute(
            select(ConversationDynamicLorebook).where(
                ConversationDynamicLorebook.user_id == user_id,
                ConversationDynamicLorebook.conversation_id == conversation_id,
                ConversationDynamicLorebook.name == name,
            )
        )
    ).scalar_one_or_none()


async def _find_entry(db: AsyncSession, lorebook_id: str, ref: str) -> LorebookEntry | None:
    """Entry by name (case-insensitive), exact-key fallback — the same rule
    the sandbox facade uses, so what a script updated is what gets updated."""
    if not ref:
        return None
    row = (
        await db.execute(
            select(LorebookEntry).where(
                LorebookEntry.lorebook_id == lorebook_id,
                func.lower(LorebookEntry.name) == ref.lower(),
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    candidates = (
        await db.execute(select(LorebookEntry).where(LorebookEntry.lorebook_id == lorebook_id))
    ).scalars().all()
    ref_lower = ref.lower()
    for cand in candidates:
        if ref_lower in [k.lower() for k in parse_json_list(cand.keys or "[]")]:
            return cand
    return None


async def _run_content_guards(
    db: AsyncSession,
    user_id: str,
    lorebook_id: str,
    fields: dict[str, Any],
    added_chars: int,
    exclude_entry_id: str | None = None,
    scan_name: str = "",
) -> str:
    """sanitize -> size -> scan, the lexicon order. Returns the refusal
    reason ("" when the op may proceed); ``fields['content']`` is replaced
    with the sanitized text in place. Only runs when the op changes content;
    unchanged content already passed these guards when it was written."""
    if "content" not in fields:
        return ""
    fields["content"] = await sanitize_entry_content(
        db, user_id, lorebook_id, fields["content"]
    )
    try:
        await enforce_lorebook_size(
            db,
            lorebook_id,
            len(fields["content"]) + max(0, added_chars),
            exclude_entry_id=exclude_entry_id,
        )
    except HTTPException as exc:
        return f"size limit: {exc.detail}"
    scan = await scan_entry(user_id, scan_name or fields.get("name") or "",
                            fields["content"], lorebook_id)
    if not scan.safe:
        return "refused by safety scan"
    return ""


async def apply_ops(
    db: AsyncSession,
    body_json: dict[str, Any],
    user_id: str,
    conversation_id: str,
    ops: list[dict[str, Any]],
    source: str,
) -> list[MatchedEntry]:
    """Apply one cantrip run's lorebook ops. Returns the applied add/update
    delta as ``MatchedEntry`` rows for same-turn injection. Never raises.

    Refusal reasons: sandbox/replay isolation, per-request op cap, malformed
    op, unknown book, oversize content (413 from the guard), critical scanner
    finding. Each refusal is logged and captured; the remaining ops apply.
    """
    from app.services import snapshots
    from app.services.debug import debug_capture

    if not ops:
        return []

    if body_json.get("_gitv_sandbox") or body_json.get("_gitv_replay"):
        debug_capture(
            body_json, "dynamic_lorebook_ops", "Dynamic Lorebook Ops",
            detail=f"Sandbox/replay run: {len(ops)} op(s) from '{source}' dropped, nothing written",
            metadata={"source": source, "dropped": len(ops)},
        )
        return []

    used = int(body_json.get(OPS_USED_KEY, 0) or 0)
    outcomes: list[dict[str, Any]] = []
    delta: list[MatchedEntry] = []

    # Phase A -- resolve and guard. SELECTs and guard sessions only; nothing
    # is added or flushed until every op in the batch has been vetted.
    resolved: dict[str, str] = {}          # book name -> lorebook_id (existing)
    pending_creates: dict[str, str] = {}   # book name -> tag, to construct
    pending_chars: dict[str, int] = {}     # lorebook id (or "" for pending) -> chars staged
    staged: list[dict[str, Any]] = []      # vetted ops, applied in phase B
    # Upsert visibility inside one batch: staged adds are not in the DB yet,
    # so a second add for the same entry must find the staged step, not the
    # table (add upserts by name; last write wins).
    staged_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    async def resolve(name: str, tag: str = "") -> str:
        if name in resolved:
            return resolved[name]
        row = await _find_dynamic_row(db, user_id, conversation_id, name)
        if row is None and tag:
            # Cross-conversation books live under the "" sentinel row.
            row = await _find_dynamic_row(db, user_id, "", name)
        if row is not None:
            resolved[name] = row.lorebook_id
        return resolved.get(name, "")

    for op in ops:
        kind = str(op.get("op") or "")
        name = str(op.get("book") or "")[:128]
        if kind not in ("create", "add", "update", "delete") or not name:
            outcomes.append({"op": kind or "?", "book": name, "outcome": "refused", "reason": "malformed op"})
            continue
        if used >= MAX_OPS_PER_REQUEST:
            outcomes.append({
                "op": kind, "book": name, "outcome": "refused",
                "reason": f"per-request op cap ({MAX_OPS_PER_REQUEST}) reached",
            })
            continue

        if kind == "create":
            tag = str(op.get("tag") or "")[:128]
            lorebook_id = await resolve(name, tag)
            if lorebook_id:
                # get-or-create: the book exists; only a newly supplied tag
                # writes anything (activation is a union, so setting a tag
                # never turns the book off anywhere).
                if tag:
                    staged.append({"kind": "set_tag", "lorebook_id": lorebook_id, "tag": tag})
                outcomes.append({"op": "create", "book": name, "outcome": "exists"})
            else:
                pending_creates[name] = tag
                outcomes.append({"op": "create", "book": name, "outcome": "created"})
            used += 1
            continue

        lorebook_id = await resolve(name)
        if not lorebook_id and name not in pending_creates:
            outcomes.append({
                "op": kind, "book": name, "outcome": "refused",
                "reason": "unknown book (create it first; lorebook_reads books are read-only)",
            })
            continue
        used += 1
        book_key = lorebook_id  # "" while the book is pending creation

        if kind == "add":
            fields = _coerce_fields(op.get("entry"), partial=False)
            if fields is None:
                outcomes.append({"op": "add", "book": name, "outcome": "refused", "reason": "entry needs content"})
                continue
            # The sandbox upserts by name-or-first-key; resolve the same way so
            # a re-run replaces its entry instead of duplicating it.
            ref = fields.get("name") or ""
            if not ref:
                keys = parse_json_list(fields.get("keys", "[]"))
                ref = keys[0] if keys else ""
            staged_hit = staged_by_key.get((book_key, ref.lower()))
            existing = staged_hit["existing"] if staged_hit else (
                await _find_entry(db, lorebook_id, ref) if lorebook_id else None
            )
            reason = await _run_content_guards(
                db, user_id, lorebook_id, fields,
                pending_chars.get(book_key, 0),
                exclude_entry_id=existing.id if existing else None,
            )
            if reason:
                outcomes.append({"op": "add", "book": name, "outcome": "refused", "reason": reason})
                continue
            pending_chars[book_key] = pending_chars.get(book_key, 0) + len(fields.get("content", ""))
            if staged_hit is not None:
                # Same entry staged earlier in this batch: last write wins.
                staged_hit["fields"] = fields
                outcomes.append({"op": "add", "book": name, "outcome": "updated"})
                continue
            step = {"kind": "upsert", "book": name, "lorebook_id": lorebook_id,
                    "existing": existing, "fields": fields}
            staged.append(step)
            staged_by_key[(book_key, ref.lower())] = step
            outcomes.append({
                "op": "add", "book": name, "outcome": "updated" if existing else "added",
            })
            continue

        if kind == "update":
            ref = str(op.get("entry") or "")
            fields = _coerce_fields(op.get("fields"), partial=True)
            if not ref or fields is None:
                outcomes.append({"op": "update", "book": name, "outcome": "refused", "reason": "malformed update"})
                continue
            staged_hit = staged_by_key.get((book_key, ref.lower()))
            existing = staged_hit["existing"] if staged_hit else (
                await _find_entry(db, lorebook_id, ref) if lorebook_id else None
            )
            if existing is None and staged_hit is None:
                outcomes.append({"op": "update", "book": name, "outcome": "refused",
                                 "reason": f"no entry '{ref}'"})
                continue
            # Guards check only what changes: content that is not being
            # rewritten already passed them when it was written.
            reason = await _run_content_guards(
                db, user_id, lorebook_id, fields,
                pending_chars.get(book_key, 0),
                exclude_entry_id=existing.id if existing else None,
                scan_name=(existing.name if existing else "") or ref,
            )
            if reason:
                outcomes.append({"op": "update", "book": name, "outcome": "refused", "reason": reason})
                continue
            pending_chars[book_key] = pending_chars.get(book_key, 0) + len(fields.get("content", ""))
            if staged_hit is not None:
                # An add staged earlier this batch: merge over it (last write
                # wins) instead of staging a second mutation.
                staged_hit["fields"] = {**staged_hit["fields"], **fields}
                outcomes.append({"op": "update", "book": name, "outcome": "updated"})
                continue
            staged.append({"kind": "update", "book": name, "lorebook_id": lorebook_id,
                           "existing": existing, "fields": fields})
            outcomes.append({"op": "update", "book": name, "outcome": "updated"})
            continue

        # delete
        ref = str(op.get("entry") or "")
        existing = await _find_entry(db, lorebook_id, ref) if lorebook_id else None
        if existing is None:
            outcomes.append({"op": "delete", "book": name, "outcome": "refused",
                             "reason": f"no entry '{ref}'"})
            continue
        staged.append({"kind": "delete", "book": name, "lorebook_id": lorebook_id,
                       "existing": existing})
        outcomes.append({"op": "delete", "book": name, "outcome": "deleted"})

    # Phase B -- construct and mutate. Every guard has run; flushes are safe
    # now, and one commit at the end lands snapshots and audit rows with it.
    # All attribute reads happen before the commit (async sessions expire
    # objects on commit, and a lazy refresh off the event loop would raise).
    book_name_by_id: dict[str, str] = {}
    descriptions = {
        str(o.get("book")): str(o.get("description") or "")[:2000]
        for o in ops if o.get("op") == "create"
    }
    new_books: list[Lorebook] = []
    for name, tag in pending_creates.items():
        book = Lorebook(
            user_id=user_id,
            name=name,
            description=descriptions.get(name, ""),
            is_active=False,
            tag=tag,
        )
        db.add(book)
        new_books.append(book)
    if new_books:
        await db.flush()
    for book, (name, tag) in zip(new_books, pending_creates.items()):
        db.add(ConversationDynamicLorebook(
            user_id=user_id,
            conversation_id="" if tag else conversation_id,
            name=name,
            lorebook_id=book.id,
        ))
        resolved[name] = book.id
        book_name_by_id[book.id] = name
        logger.info("Dynamic lorebook '%s' created by '%s' (tag=%r)", name, source, tag or "")

    staged_finals: list[dict[str, Any]] = []
    for step in staged:
        kind = step["kind"]
        if kind == "set_tag":
            book = (
                await db.execute(select(Lorebook).where(Lorebook.id == step["lorebook_id"]))
            ).scalar_one_or_none()
            if book is not None and not book.tag:
                book.tag = step["tag"]
            continue
        lorebook_id = step["lorebook_id"] or resolved.get(step["book"], "")
        book_name = book_name_by_id.get(lorebook_id) or step["book"]
        existing: LorebookEntry | None = step.get("existing")
        if existing is not None and kind in ("update", "delete"):
            await snapshots.capture(db, user_id, "lorebook", lorebook_id)
        if kind == "delete":
            await db.delete(existing)
            continue
        fields: dict[str, Any] = step["fields"]
        if existing is not None:
            staged_finals.append({
                "content": fields.get("content", existing.content),
                "position": fields.get("position", existing.position),
                "insertion_order": fields.get("insertion_order", existing.insertion_order),
                "name": fields.get("name", existing.name),
                "entry_id": existing.id or "",
                "lorebook_id": lorebook_id,
                "lorebook_name": book_name,
            })
            for key, value in fields.items():
                setattr(existing, key, value)
        else:
            entry = LorebookEntry(
                lorebook_id=lorebook_id,
                name=fields.get("name", ""),
                keys=fields.get("keys", "[]"),
                secondary_keys=fields.get("secondary_keys", "[]"),
                content=fields.get("content", ""),
                position=fields.get("position", _VALID_POSITIONS[1]),
                insertion_order=fields.get("insertion_order", 10),
                is_constant=fields.get("is_constant", False),
                is_selective=fields.get("is_selective", False),
                character_limit=fields.get("character_limit", 0),
            )
            db.add(entry)
            staged_finals.append({
                "content": entry.content,
                "position": entry.position,
                "insertion_order": entry.insertion_order,
                "name": entry.name,
                # Assigned at flush below; kept current by reference.
                "entry_id": "",
                "entry_ref": entry,
                "lorebook_id": lorebook_id,
                "lorebook_name": book_name,
            })

    # One flush assigns ids to the new entries so the delta carries real
    # provenance for the per-entry metrics rows.
    await db.flush()
    for final in staged_finals:
        ref = final.pop("entry_ref", None)
        if ref is not None:
            final["entry_id"] = ref.id or ""
        delta.append(MatchedEntry(
            content=str(final["content"]),
            position=str(final["position"]),
            insertion_order=int(final["insertion_order"]),
            name=str(final["name"]),
            lorebook_id=str(final["lorebook_id"]),
            lorebook_name=str(final["lorebook_name"]),
            entry_id=str(final["entry_id"]),
        ))

    await db.commit()
    body_json[OPS_USED_KEY] = used

    applied = sum(1 for o in outcomes if o["outcome"] in
                  ("added", "updated", "deleted", "created", "exists"))
    refused = sum(1 for o in outcomes if o["outcome"] == "refused")
    debug_capture(
        body_json, "dynamic_lorebook_ops", "Dynamic Lorebook Ops",
        detail=f"{applied} op(s) applied, {refused} refused from '{source}'",
        metadata={"source": source, "outcomes": outcomes},
    )
    logger.info(
        "Dynamic lorebook ops from '%s': %d applied, %d refused", source, applied, refused
    )
    return delta
