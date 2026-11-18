"""Phase 29 — Chat Lexicon compiler and Cantrip Draft queue.

Both halves are triggered by in-chat action commands (`<LEXICON:name>`,
`<CANTRIP:description>`), parsed and stripped in `command_tags.py` before the
request is forwarded. This module is the Python side of both:

- `run_lexicon_compile`: one side LLM call that profiles a named thing from
  the conversation and files it as an entry in a per-chat lorebook bound by
  conversation identity (the fifth activation source).
- `handle_cantrip_request`: writes a pending draft row for the Assistant pane,
  or — only when the admin ALLOW and user ENABLE gates are both on — drafts,
  validates and installs the cantrip in-request.

Every failure degrades to "forward untouched, write nothing": a compile that
cannot run costs the user an absent entry, never a failed reply. Sandbox and
replay runs never write.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.cantrip import Cantrip
from app.models.cantrip_request import CantripRequest
from app.models.conversation_lorebook_binding import ConversationLorebookBinding
from app.models.conversation_summary import ConversationSummary
from app.models.endpoint import Endpoint
from app.models.lorebook import Lorebook
from app.models.lorebook_entry import LorebookEntry
from app.models.user_settings import UserSettings
from app.services import snapshots
from app.services.admin import get_admin_settings
from app.services.cantrip import test_cantrip
from app.services.content_guard import check_size, log_scan_findings
from app.services.deno_runner import validate_cantrip_syntax
from app.services.llm_params import apply_to_body, parse_params, resolve
from app.services.lorebook_writes import (
    enforce_lorebook_size,
    sanitize_entry_content,
    scan_entry,
)
from app.services.safety_scanner import scan_cantrip

logger = logging.getLogger(__name__)

TRANSCRIPT_CAP = 24000
SYSTEM_PROMPT_CAP = 4000
CONTEXT_SNAPSHOT_CAP = 12000

LEXICON_SYSTEM_PROMPT = (
    "You compile a lorebook entry for one named thing in a roleplay "
    "conversation -- a character, creature, item, spell, place or faction. "
    "Pull together everything the conversation establishes about it. Where "
    "detail is missing, invent plausible background consistent with the "
    "setting's tone, and mark inferred detail with '(inferred)'. Return JSON "
    'only, no prose around it: {"name": string, "aliases": [string], '
    '"type": string, "keys": [string], "secondary_keys": [string], '
    '"content": string}. `content` is the full entry text, self-contained '
    "enough that a model reading only this entry understands the thing. "
    "`keys` are the words or phrases that should make the entry surface in "
    "later scenes (lowercased, include the name itself)."
)

NOTICE_QUEUE = "cantrip draft queued — review it in the Assistant pane."


def build_notice(text: str) -> str:
    """An OOC notice GitV appends to a reply. Only notices carrying the GitV
    marker are stripped from later requests (command_tags.GITV_NOTICE_PATTERN)
    -- a blanket [OOC: ...] strip would eat player-authored OOC notes.
    """
    return f"[OOC: GitV: {text}]"


def _writes_isolated(body_json: dict[str, Any]) -> bool:
    """Sandbox and replay runs compile and draft read-only, like cantrip
    stores: a re-run must not rewrite lorebooks or re-install cantrips."""
    return bool(body_json.get("_gitv_sandbox") or body_json.get("_gitv_replay"))


def resolve_action_identity(
    request_headers: dict[str, str], internal_chat_id: str
) -> tuple[str, str]:
    """Conversation identity for bindings and queue rows: the client header
    always wins; the internal rolling-hash id is the fallback for platforms
    that send none.
    """
    header_id = (request_headers.get("x-conversation-id") or request_headers.get("x-chat-id") or "").strip()
    if header_id:
        return header_id, "client_header"
    return internal_chat_id, "rolling_hash"


def lexicon_parameters(
    user_settings: UserSettings | None, endpoint: Endpoint | None, model: str
) -> dict[str, Any]:
    """Resolve the lexicon call's parameters.

    Layers, broadest first: the user's lexicon settings, the endpoint the
    compile will actually run on, that endpoint's entry for the model in play.
    The lexicon layer is the closest thing to the message, so it wins.
    """
    from app.services.routing import endpoint_parameters, model_parameter_map

    layers = []
    if user_settings is not None:
        layers.append((
            "user settings (lexicon)",
            parse_params(user_settings.lexicon_parameters_json, "user settings"),
        ))
    if endpoint is not None:
        layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
        layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
    return dict(resolve(layers).values)


async def load_lexicon_endpoints(
    db: AsyncSession, user_id: str
) -> tuple[UserSettings | None, list[Endpoint]]:
    """Ordered endpoint chain for lexicon work: the dedicated lexicon pick,
    else the summarizer's endpoint (same long-context summarizing shape), else
    the default endpoint, else the first enabled endpoint. Deduplicated and
    enabled-only; each candidate is a failover hop.

    A user with no settings row yet (never opened Settings) gets the defaults:
    enabled, first-enabled-endpoint chain. The enabled check lives in the
    caller so it can distinguish "disabled" from "no row".
    """
    settings = (
        await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    ).scalar_one_or_none()

    candidate_ids = []
    if settings is not None:
        candidate_ids = [
            settings.lexicon_endpoint_id,
            settings.summarization_endpoint_id,
            settings.default_endpoint_id,
        ]
    chain: list[Endpoint] = []
    if any(candidate_ids):
        rows = (
            await db.execute(
                select(Endpoint).where(Endpoint.id.in_([c for c in candidate_ids if c]))
            )
        ).scalars().all()
        by_id = {e.id: e for e in rows if e.enabled}
        for cid in candidate_ids:
            ep = by_id.get(cid or "")
            if ep is not None and ep not in chain:
                chain.append(ep)

    if not chain:
        first_enabled = (
            await db.execute(
                select(Endpoint)
                .where(Endpoint.user_id == user_id, Endpoint.enabled.is_(True))
                .order_by(Endpoint.priority, Endpoint.created_at)
            )
        ).scalars().first()
        if first_enabled is not None:
            chain.append(first_enabled)

    return settings, chain


def _format_transcript(messages: list[dict[str, Any]]) -> str:
    from app.services.summarization import _format_transcript as fmt

    return fmt(messages)[:TRANSCRIPT_CAP]


def _system_prompt_excerpt(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", "")) for p in content if isinstance(p, dict)
                )
            return str(content)[:SYSTEM_PROMPT_CAP]
    return ""


async def _side_llm_call(
    body_json: dict[str, Any],
    user_id: str,
    settings: UserSettings,
    chain: list[Endpoint],
    messages: list[dict[str, Any]],
    label: str,
) -> tuple[str | None, dict[str, Any]]:
    """One LLM call across the failover chain, recorded per attempt.

    Returns (content, info) — content is None when every candidate failed.
    Never raises: callers degrade on failure.
    """
    from app.services.debug import debug_capture, debug_capture_response
    from app.services.debug_metrics import PURPOSE_LEXICON, record_llm_call
    from app.services.proxy import _build_upstream_url, _do_forward

    for attempt, endpoint in enumerate(chain):
        model = endpoint.default_model or "gpt-4"
        configured = lexicon_parameters(settings, endpoint, model)
        url = _build_upstream_url(
            endpoint.base_url, "/v1/chat/completions", endpoint.api_base_path or ""
        )
        headers = {
            "Authorization": f"Bearer {endpoint.api_key}",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        body = apply_to_body(body, configured)

        started = time.monotonic()
        error = ""
        data: dict[str, Any] | None = None
        status_code = 0
        try:
            data, status_code = await _do_forward(
                "POST", url, headers, json.dumps(body).encode(),
                httpx.Timeout(120.0, connect=15.0),
                provider=endpoint.provider or "",
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
                configured=configured,
            )
            if status_code == 200 and isinstance(data, dict):
                content = ""
                choices = data.get("choices") or []
                if choices:
                    message = choices[0].get("message") or {}
                    content = str(message.get("content") or "")
                if content.strip():
                    record_llm_call(
                        body_json, purpose=PURPOSE_LEXICON,
                        latency_ms=(time.monotonic() - started) * 1000.0,
                        status_code=status_code, response_data=data,
                        messages=body.get("messages"),
                        endpoint_id=endpoint.id, endpoint_name=endpoint.name,
                        provider=endpoint.provider or "",
                        model_requested=model,
                        failover_attempt=attempt,
                    )
                    debug_capture_response(
                        body_json, f"{label}_llm", f"{label} (LLM)",
                        detail=f"Compiled on '{endpoint.name}'"
                        + (f" after {attempt} failover(s)" if attempt else ""),
                        content_after=content[:2000],
                        metadata={
                            "endpoint": endpoint.name, "model": model,
                            "failover_attempt": attempt,
                        },
                    )
                    return content, {"endpoint": endpoint.name, "model": model, "attempts": attempt + 1}
            error = f"status {status_code}"
        except Exception as exc:  # noqa: BLE001 -- degrade, never fail the reply
            error = f"{type(exc).__name__}: {exc}"[:300]

        record_llm_call(
            body_json, purpose=PURPOSE_LEXICON,
            latency_ms=(time.monotonic() - started) * 1000.0,
            status_code=status_code, response_data=data,
            messages=body.get("messages"),
            endpoint_id=endpoint.id, endpoint_name=endpoint.name,
            provider=endpoint.provider or "",
            model_requested=model,
            failover_attempt=attempt,
            error=error,
        )

    debug_capture(
        body_json, f"{label}_error", f"{label}",
        detail=f"Did not run: every endpoint failed ({error}). Nothing written.",
        metadata={"error": error},
    )
    return None, {"error": error}


def _parse_profile(text: str) -> dict[str, Any] | None:
    """Brace-slice JSON parse, the same contract the verification judge uses."""
    raw = text
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        logger.warning("Lexicon: unparseable profile output: %s", raw[:200])
        return None
    if not isinstance(data, dict):
        return None
    return data


def _profile_to_entry_fields(name_arg: str, profile: dict[str, Any]) -> dict[str, Any] | None:
    content = str(profile.get("content") or "").strip()
    if not content:
        return None
    name = str(profile.get("name") or "").strip() or name_arg
    keys = [str(k) for k in profile.get("keys") or [] if str(k).strip()]
    if not keys:
        keys = [name.lower()]
    if name.lower() not in [k.lower() for k in keys]:
        keys.insert(0, name.lower())
    secondary = [str(k) for k in profile.get("secondary_keys") or [] if str(k).strip()]
    return {
        "name": name[:128],
        "keys": json.dumps(keys),
        "secondary_keys": json.dumps(secondary),
        "content": content,
        "position": "before_last_message",
        "insertion_order": 100,
        "is_constant": False,
        "is_selective": False,
        "is_disabled": False,
        "character_limit": 0,
    }


async def _find_matching_entry(
    db: AsyncSession, lorebook_id: str, names: list[str]
) -> LorebookEntry | None:
    for name in names:
        if not name:
            continue
        row = (
            await db.execute(
                select(LorebookEntry).where(
                    LorebookEntry.lorebook_id == lorebook_id,
                    func.lower(LorebookEntry.name) == name.lower(),
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


async def _upsert_lexicon_entry(
    user_id: str,
    conversation_id: str,
    identity_source: str,
    internal_chat_id: str,
    fields: dict[str, Any],
) -> str:
    """Create or revise the chat's lexicon lorebook entry. Returns the outcome
    ("created", "updated", "refused" or "skipped"); raises nothing — a refused
    or failed write leaves the lorebook untouched.
    """
    async with async_session() as db:
        binding = (
            await db.execute(
                select(ConversationLorebookBinding).where(
                    ConversationLorebookBinding.user_id == user_id,
                    ConversationLorebookBinding.conversation_id == conversation_id,
                )
            )
        ).scalar_one_or_none()

        existing_entry = None
        if binding is not None:
            existing_entry = await _find_matching_entry(
                db, binding.lorebook_id, [fields["name"]]
            )

        # Guards run before anything is constructed: a refused or oversize
        # entry must leave no half-created lorebook behind (design: no
        # blank-first creation).
        content = await sanitize_entry_content(db, user_id, binding.lorebook_id if binding else "", fields["content"])
        exclude_id = existing_entry.id if existing_entry else None
        lorebook_id = binding.lorebook_id if binding else ""
        await enforce_lorebook_size(db, lorebook_id, len(content), exclude_entry_id=exclude_id)
        scan = await scan_entry(user_id, fields["name"], content, lorebook_id)
        if not scan.safe:
            logger.warning(
                "Lexicon entry for '%s' refused by safety scan; nothing written", fields["name"]
            )
            return "refused"

        fields = {**fields, "content": content}

        if binding is None:
            lorebook = Lorebook(
                user_id=user_id,
                name=f"Chat Lexicon ({conversation_id[:8]})",
                description="Compiled from this conversation by <LEXICON:...> commands.",
                is_active=False,
                tag="",
            )
            db.add(lorebook)
            # Column defaults fire at flush, not construction, so the id the
            # binding and entry need does not exist until now. All guards ran
            # before anything was added; nothing else intervenes before commit.
            await db.flush()
            db.add(ConversationLorebookBinding(
                user_id=user_id,
                conversation_id=conversation_id,
                identity_source=identity_source,
                lorebook_id=lorebook.id,
            ))
            db.add(LorebookEntry(lorebook_id=lorebook.id, **fields))
            outcome = "created"
        else:
            if existing_entry is not None:
                await snapshots.capture(db, user_id, "lorebook", binding.lorebook_id)
                for key, value in fields.items():
                    setattr(existing_entry, key, value)
                outcome = "updated"
            else:
                db.add(LorebookEntry(lorebook_id=binding.lorebook_id, **fields))
                outcome = "created"

        await db.commit()
        return outcome


async def run_lexicon_compile(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    name_arg: str,
) -> dict[str, Any]:
    """The 29a stage: compile a profile for `name_arg` and file it.

    Called after conversation summarization so the extraction sees memory,
    triggered lorebook and skill injections plus the compressed history. Any
    failure degrades to "nothing written" — the entry simply does not exist
    yet.
    """
    from app.services.debug import debug_capture

    result: dict[str, Any] = {"name": name_arg, "status": "skipped"}

    if _writes_isolated(body_json):
        result["reason"] = "sandbox/replay run"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Skipped: sandbox/replay run writes nothing",
                      metadata=result)
        return result

    conversation_id, identity_source = resolve_action_identity(request_headers, internal_chat_id)
    result["conversation_id"] = conversation_id[:12]

    messages = body_json.get("messages", [])
    transcript = _format_transcript(messages)
    if not transcript.strip():
        result["reason"] = "empty transcript"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Skipped: nothing to read", metadata=result)
        return result

    async with async_session() as db:
        settings, chain = await load_lexicon_endpoints(db, user_id)
        summary_row = (
            await db.execute(
                select(ConversationSummary).where(
                    ConversationSummary.user_id == user_id,
                    ConversationSummary.internal_chat_id == internal_chat_id,
                )
            )
        ).scalar_one_or_none()
        existing_entry = None
        binding = (
            await db.execute(
                select(ConversationLorebookBinding).where(
                    ConversationLorebookBinding.user_id == user_id,
                    ConversationLorebookBinding.conversation_id == conversation_id,
                )
            )
        ).scalar_one_or_none()
        if binding is not None:
            existing_entry = await _find_matching_entry(db, binding.lorebook_id, [name_arg])

    if settings is not None and not settings.lexicon_enabled:
        result["reason"] = "lexicon disabled"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Skipped: lexicon disabled in settings", metadata=result)
        return result
    if not chain:
        result["reason"] = "no enabled endpoint"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Skipped: no enabled endpoint for the lexicon compiler",
                      metadata=result)
        return result

    user_parts = [f"Compile a lorebook entry for: {name_arg}"]
    system_excerpt = _system_prompt_excerpt(messages)
    if system_excerpt:
        user_parts.append(f"---\nCurrent system prompt (post-injection excerpt):\n{system_excerpt}")
    if summary_row is not None and summary_row.summary:
        user_parts.append(f"---\nConversation summary so far:\n{summary_row.summary}")
    if existing_entry is not None and existing_entry.content:
        user_parts.append(
            f"---\nExisting entry for {name_arg} (revise rather than reset):\n{existing_entry.content}"
        )
    user_parts.append(f"---\nConversation transcript:\n{transcript}\n---\nReturn JSON only.")

    try:
        content, info = await _side_llm_call(
            body_json, user_id, settings, chain,
            [
                {"role": "system", "content": LEXICON_SYSTEM_PROMPT},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            label="lexicon",
        )
    except Exception:
        logger.exception("Lexicon compile failed for '%s'", name_arg)
        content = None

    if not content:
        result["status"] = "failed"
        result["reason"] = "extraction did not run or returned empty"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Failed: no usable model output; nothing written", metadata=result)
        return result

    profile = _parse_profile(content)
    fields = _profile_to_entry_fields(name_arg, profile) if profile else None
    if fields is None:
        result["status"] = "failed"
        result["reason"] = "unparseable output"
        debug_capture(body_json, "lexicon_compiler", "Lexicon Compiler",
                      detail="Failed: output did not parse to a usable entry; nothing written",
                      metadata=result)
        return result

    try:
        outcome = await _upsert_lexicon_entry(
            user_id, conversation_id, identity_source, internal_chat_id, fields
        )
    except Exception:
        # Guard refusals (413 size) and anything else: nothing was written.
        logger.exception("Lexicon entry write failed for '%s'", fields["name"])
        outcome = "skipped"

    result["status"] = outcome
    result["entry"] = fields["name"]
    result.update({k: v for k, v in info.items() if k != "error"})
    debug_capture(
        body_json, "lexicon_compiler", "Lexicon Compiler",
        detail={
            "created": f"Entry '{fields['name']}' filed in the chat's lexicon lorebook",
            "updated": f"Entry '{fields['name']}' revised",
            "refused": f"Entry '{fields['name']}' refused by the safety scan; nothing written",
        }.get(outcome, f"Entry not written ({outcome})"),
        metadata=result,
    )
    return result


def _context_snapshot(messages: list[dict[str, Any]]) -> str:
    return _format_transcript(messages)[:CONTEXT_SNAPSHOT_CAP]


async def _auto_install_gates(user_id: str) -> bool:
    """Admin must ALLOW and the user must ENABLE. Both default off."""

    admin = await get_admin_settings()
    if not admin.assistant_cantrip_auto_install_allowed:
        return False
    async with async_session() as db:
        settings = (
            await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        ).scalar_one_or_none()
    return bool(settings and settings.cantrip_auto_install_enabled)


async def _draft_cantrip(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    settings: UserSettings | None,
    chain: list[Endpoint],
    request_text: str,
) -> tuple[str, str, str]:
    """One LLM call that drafts the cantrip code.

    Returns (code, name, error). Never raises.
    """
    from pathlib import Path

    docs_path = Path(__file__).parent / "assistant" / "docs" / "cantrip-context-api.md"
    try:
        cantrip_docs = docs_path.read_text(encoding="utf-8")[:16000]
    except OSError:
        cantrip_docs = "(cantrip API documentation unavailable)"

    if settings is None or not chain:
        return "", "", "no enabled endpoint"

    system = (
        "You write GitInTheVan cantrips: sandboxed JavaScript executed with a "
        "`context` object, no I/O, no network, no imports. Return JSON only: "
        '{"name": string, "code": string}. The code must be a plain script '
        "(not a function declaration) that operates on `context`."
    )
    user = (
        f"Cantrip API reference:\n{cantrip_docs}\n---\n"
        f"Requested cantrip: {request_text}\n---\n"
        f"Conversation context it will run in:\n{_context_snapshot(body_json.get('messages', []))}\n---\n"
        "Write the cantrip. Return JSON only."
    )
    content, _ = await _side_llm_call(
        body_json, user_id, settings, chain,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        label="cantrip_draft",
    )
    if not content:
        return "", "", "drafting model call failed"
    data = _parse_profile(content)
    if not data or not str(data.get("code") or "").strip():
        return "", "", "draft output unparseable"
    name = str(data.get("name") or "").strip()[:128] or request_text[:40]
    return str(data["code"]), name, ""


async def _try_auto_install(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    request_text: str,
) -> str:
    """Draft, validate, scan, smoke-test and install a cantrip in-request.

    Gates, in order: Deno syntax parse, size cap + safety scan (critical
    findings refuse outright), sandbox smoke run (failure installs the cantrip
    disabled with findings). Returns the player-facing notice text.
    """
    from app.services.debug import debug_capture

    findings: list[str] = []
    async with async_session() as db:
        settings, chain = await load_lexicon_endpoints(db, user_id)

    code, name, error = await _draft_cantrip(
        body_json, user_id, request_headers, internal_chat_id, settings, chain, request_text
    )
    if not code:
        await _record_request(user_id, request_headers, internal_chat_id, request_text,
                              body_json, status="failed",
                              note=f"Drafting failed: {error}")
        return f"cantrip draft failed ({error}); see the Assistant pane."

    # Sync subprocess -- keep it off the event loop like every other Deno call.
    import asyncio

    syntax_error = await asyncio.get_running_loop().run_in_executor(
        None, validate_cantrip_syntax, code
    )
    if syntax_error:
        await _record_request(user_id, request_headers, internal_chat_id, request_text,
                              body_json, status="failed",
                              note=f"Syntax invalid: {syntax_error}")
        return "cantrip draft refused (invalid syntax); see the Assistant pane."

    admin = await get_admin_settings()
    try:
        check_size(code, admin.max_script_size_kb * 1024, "Cantrip code")
    except Exception:
        await _record_request(user_id, request_headers, internal_chat_id, request_text,
                              body_json, status="failed", note="Code exceeded the size cap")
        return "cantrip draft refused (size); see the Assistant pane."

    scan = scan_cantrip(code)
    await log_scan_findings(user_id, scan, "cantrip", "")
    if not scan.safe:
        await _record_request(user_id, request_headers, internal_chat_id, request_text,
                              body_json, status="failed",
                              note="Critical safety-scan findings: " + scan.summary)
        return "cantrip draft refused (safety scan); see the Assistant pane."

    smoke_ok = True
    try:
        synthetic = {
            "chat": {
                "last_message": "Hello", "last_messages": [{"message": "Hello"}],
                "message_count": 1, "conversation_id": "smoke", "user_name": "User",
                "message_created_at": "",
            },
            "character": {"name": "Test", "personality": "", "scenario": "",
                          "example_dialogs": "", "description": "", "first_message": "",
                          "alternate_greetings": []},
        }
        result = await test_cantrip(code, synthetic, timeout_ms=5000)
        if result.has_error or (result.error or "").strip():
            smoke_ok = False
            findings.append(f"smoke run error: {result.error}")
    except Exception as exc:  # noqa: BLE001 -- a failed smoke run installs disabled
        smoke_ok = False
        findings.append(f"smoke run raised: {type(exc).__name__}")

    is_active = smoke_ok
    async with async_session() as db:
        cantrip = Cantrip(
            user_id=user_id,
            name=name or "Drafted cantrip",
            description=request_text[:2000],
            code=code,
            run_pre_driver=True,
            is_active=is_active,
        )
        db.add(cantrip)
        await db.commit()
        cantrip_id = cantrip.id

    note = "installed enabled: all gates passed" if is_active else (
        "installed disabled: " + "; ".join(findings)
    )
    await _record_request(user_id, request_headers, internal_chat_id, request_text,
                          body_json, status="installed",
                          note=f"{note} (cantrip {cantrip_id[:8]})")
    debug_capture(body_json, "cantrip_auto_install", "Cantrip Auto-Install",
                  detail=f"Cantrip '{name}' {note}",
                  metadata={"cantrip_id": cantrip_id, "active": is_active})
    if is_active:
        return f"cantrip '{name}' installed and enabled."
    return f"cantrip '{name}' installed disabled ({'; '.join(findings)}); see the Assistant pane."


async def _record_request(
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    request_text: str,
    body_json: dict[str, Any],
    status: str,
    note: str = "",
) -> None:
    conversation_id, _ = resolve_action_identity(request_headers, internal_chat_id)
    async with async_session() as db:
        db.add(CantripRequest(
            user_id=user_id,
            conversation_id=conversation_id,
            request_text=request_text[:2000],
            context_text=_context_snapshot(body_json.get("messages", [])),
            status=status,
            outcome_note=note[:2000],
        ))
        await db.commit()


async def handle_cantrip_request(
    body_json: dict[str, Any],
    user_id: str,
    request_headers: dict[str, str],
    internal_chat_id: str,
    request_text: str,
) -> None:
    """The 29b entry point: queue a draft for the pane, or auto-install when
    both gates are on. Appends an OOC notice the player sees in the reply.
    """
    from app.services.debug import debug_capture

    if _writes_isolated(body_json):
        debug_capture(body_json, "cantrip_request", "Cantrip Draft",
                      detail="Skipped: sandbox/replay run writes nothing",
                      metadata={"request": request_text[:100]})
        return

    if await _auto_install_gates(user_id):
        notice = await _try_auto_install(
            body_json, user_id, request_headers, internal_chat_id, request_text
        )
    else:
        await _record_request(user_id, request_headers, internal_chat_id,
                              request_text, body_json, status="pending")
        notice = NOTICE_QUEUE
        debug_capture(body_json, "cantrip_request", "Cantrip Draft",
                      detail="Queued for the Assistant pane",
                      metadata={"request": request_text[:200]})

    body_json.setdefault("_gitv_pending_notices", []).append(build_notice(notice))
