"""Phase 17 — Passenger: scripted LLM calls from inside cantrips.

Cantrips stay no-I/O in the Deno sandbox; Passenger is the single sanctioned
egress. A script calls ``context.passenger.call({tag, instructions, context,
model})``; the runner shuttles the request to the host over a sentinel-framed
stderr/stdin protocol (see ``deno_runner.py``), and this module executes it:

- gates (admin switch -> user switch, both snapshotted once per request,
  re-checked per call), then the rule-22 two-half per-request call budget
  (effective = min(admin cap, user budget when > 0)), then payload caps;
- transport through ``_do_forward`` over a failover chain (Phase 24 semantics:
  provider/LiteLLM aware, per-attempt parameter re-resolution, failover on any
  status != 200 or exception), so Passenger never recreates the off-Driver bug
  class Phase 24 fixed;
- instrumentation per rules 16 and 30: ``PURPOSE_PASSENGER`` metrics rows on
  every attempt and ``passenger_call_{n}`` / ``passenger_result_{n}`` /
  ``passenger_error_{n}`` debug captures on every path.

Every failure resolves to ``None`` for the script and a forwarded, untouched
request. The module never raises out of ``call()``.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.services.debug_metrics import PURPOSE_PASSENGER, record_llm_call
from app.services.llm_params import apply_to_body, parse_params, resolve

logger = logging.getLogger(__name__)

# Rule 12: code constants, not admin settings. The caps that guard traffic and
# spend (call count, timeout) are the two-half rule-22 settings; payload sizes
# are structural limits and stay here.
INSTRUCTIONS_CAP = 8000
CONTEXT_CAP = 24000
RESULT_CAP = 65536
RESULT_TRUNCATION_MARKER = "\n[... truncated by GitV passenger result cap]"
WATCHDOG_SLACK_MS = 5000

_HANDLER_KEY = "_gitv_passenger_handler"


class PassengerHandler:
    """Per-request Passenger state: gate snapshot, shared budget counter and
    the transport. One instance per pipeline request, stashed on ``body_json``
    so every cantrip position (pre/post-Driver, driver-callable, each map
    stage) shares one budget — the cap is what stops stage-bound cantrips from
    multiplying side-call cost N+1 times.
    """

    def __init__(
        self,
        body_json: dict[str, Any],
        user_id: str,
        *,
        enabled: bool,
        max_calls: int,
        timeout_seconds: int,
        passenger_parameters_json: str,
    ) -> None:
        self.body_json = body_json
        self.user_id = user_id
        self.enabled = enabled
        self.max_calls = max_calls
        self.timeout_seconds = timeout_seconds
        self._parameters_json = passenger_parameters_json
        # Budget counter (accepted calls, failures included — a failed attempt
        # still spent an upstream call) and the capture-name sequence.
        self._used = 0
        self._seq = 0
        # Set by map-stage cantrip passes so LLM-call rows can name the stage.
        self.stage_index: int | None = None

    def watchdog_ms(self, base_timeout_ms: int) -> int:
        """Total process timeout for a cantrip run carrying this handler:
        the script's own budget plus every Passenger call's upstream timeout,
        plus slack. Without this the cantrip default 5s would kill every
        Passenger run before the first call completes."""
        return (
            base_timeout_ms
            + self.max_calls * self.timeout_seconds * 1000
            + WATCHDOG_SLACK_MS
        )

    def _capture_error(self, n: int, reason: str, detail: str) -> None:
        from app.services.debug import debug_capture

        debug_capture(
            self.body_json, f"passenger_error_{n}", f"Passenger Call {n}",
            detail=detail,
            metadata={"reason": reason, "calls_used": self._used, "max_calls": self.max_calls},
        )

    async def call(
        self, *, tag: str, instructions: str, context: str, model: str
    ) -> tuple[str | None, str]:
        """Execute one Passenger call. Returns ``(text, summary)`` on success
        or ``(None, error_code)`` on any failure. Never raises."""
        from app.services.debug import debug_capture, debug_capture_response

        self._seq += 1
        n = self._seq

        # Host-side re-check of the gates baked into the sandbox context. The
        # template short-circuits when disabled, so reaching here with the
        # feature off means the flag changed mid-run or a protocol mismatch.
        if not self.enabled:
            self._capture_error(n, "disabled", "Passenger is disabled (admin or user toggle).")
            return None, "disabled"
        if len(instructions) > INSTRUCTIONS_CAP or len(context) > CONTEXT_CAP:
            self._capture_error(
                n, "oversize",
                f"Payload over cap (instructions {len(instructions)}/{INSTRUCTIONS_CAP}, "
                f"context {len(context)}/{CONTEXT_CAP} chars).",
            )
            return None, "oversize"

        if self._used >= self.max_calls:
            self._capture_error(
                n, "cap",
                f"Per-request Passenger call cap reached ({self.max_calls} call(s)).",
            )
            return None, "cap"

        self._used += 1
        debug_capture(
            self.body_json, f"passenger_call_{n}", f"Passenger Call {n}",
            detail=(
                f"tag '{tag or '(default)'}', model '{model or '(endpoint default)'}', "
                f"instructions {len(instructions)} chars, context {len(context)} chars "
                f"(call {self._used} of {self.max_calls})"
            ),
            metadata={
                "tag": tag, "model": model,
                "instructions_excerpt": instructions[:200],
                "context_chars": len(context),
                "call_index": n, "calls_used": self._used, "max_calls": self.max_calls,
                "stage_index": self.stage_index,
            },
        )

        text, outcome = await self._call_chain(
            tag, instructions, context, model, n
        )
        if text is not None:
            debug_capture_response(
                self.body_json, f"passenger_result_{n}", f"Passenger Call {n} (result)",
                content_after=text[:2000],
                detail=outcome,  # endpoint/model/attempts summary from the chain
                metadata={"call_index": n, "chars": len(text), "stage_index": self.stage_index},
            )
        return text, outcome

    def _layers(self, candidate: Any, model: str) -> dict[str, Any]:
        from app.services.routing import FailoverEndpoint

        layers = [(
            "user settings (passenger)",
            parse_params(self._parameters_json, "user settings"),
        )]
        if isinstance(candidate, FailoverEndpoint):
            layers.append((f"endpoint '{candidate.endpoint_name}'", candidate.parameters))
            layers.append((f"model '{model}'", candidate.params_for_model(model)))
        return dict(resolve(layers).values)

    async def _call_chain(
        self, tag: str, instructions: str, context: str, model: str, n: int
    ) -> tuple[str | None, str]:
        from app.services.proxy import _build_upstream_url, _do_forward, _flag_injection_content, auth_headers

        # Read-side injection flagging, same as lorebook/memory injections.
        await _flag_injection_content(self.user_id, instructions, f"passenger:{n}:instructions")
        await _flag_injection_content(self.user_id, context, f"passenger:{n}:context")

        async with async_session() as db:
            chain = await _resolve_chain(db, self.user_id, tag)

        if not chain:
            self._capture_error(
                n, "no_endpoint",
                f"No enabled endpoint for tag '{tag or '(default)'}'; the request forwards untouched.",
            )
            return None, "no_endpoint"

        last_error = "chain_failed"
        summary = ""
        for attempt, candidate in enumerate(chain):
            started = time.monotonic()
            call_model = model or candidate.model or "gpt-4"
            configured = self._layers(candidate, call_model)
            url = _build_upstream_url(
                candidate.base_url, "/v1/chat/completions", candidate.api_base_path or ""
            )
            headers = auth_headers(candidate.api_key)
            body: dict[str, Any] = {
                "model": call_model,
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": context or " "},
                ],
                "stream": False,
            }
            body = apply_to_body(body, configured)
            # The pipeline owns streaming for internal calls (Assistant
            # precedent): a configured `stream` parameter cannot turn a
            # Passenger call into an SSE read.
            body["stream"] = False

            data: dict[str, Any] | None = None
            status_code = 0
            error = ""
            try:
                data, status_code = await _do_forward(
                    "POST", url, headers, json.dumps(body).encode(),
                    httpx.Timeout(self.timeout_seconds, connect=15.0),
                    provider=candidate.provider or "",
                    base_url=candidate.base_url,
                    api_key=candidate.api_key,
                    configured=configured,
                )
                if status_code == 200 and isinstance(data, dict):
                    choices = data.get("choices") or []
                    content = ""
                    if choices:
                        message = choices[0].get("message") or {}
                        content = str(message.get("content") or "")
                    if content.strip():
                        if len(content) > RESULT_CAP:
                            content = content[:RESULT_CAP] + RESULT_TRUNCATION_MARKER
                        record_llm_call(
                            self.body_json, purpose=PURPOSE_PASSENGER,
                            latency_ms=(time.monotonic() - started) * 1000.0,
                            status_code=status_code, response_data=data,
                            messages=body.get("messages"),
                            stage_index=self.stage_index,
                            endpoint_id=candidate.endpoint_id,
                            endpoint_name=candidate.endpoint_name,
                            provider=candidate.provider or "",
                            model_requested=call_model,
                            failover_attempt=attempt,
                        )
                        summary = (
                            f"'{candidate.endpoint_name}' on {call_model}"
                            + (f" after {attempt} failover(s)" if attempt else "")
                            + f", {len(content)} chars"
                        )
                        return content, summary
                    error = "empty output"
                    last_error = "empty"
                else:
                    error = f"status {status_code}"
            except TimeoutError:
                error = f"timeout after {self.timeout_seconds}s"
                last_error = "timeout"
            except Exception as exc:  # noqa: BLE001 -- degrade, never fail the reply
                error = f"{type(exc).__name__}: {exc}"[:300]
                last_error = "chain_failed"

            record_llm_call(
                self.body_json, purpose=PURPOSE_PASSENGER,
                latency_ms=(time.monotonic() - started) * 1000.0,
                status_code=status_code, response_data=data,
                messages=body.get("messages"),
                stage_index=self.stage_index,
                endpoint_id=candidate.endpoint_id,
                endpoint_name=candidate.endpoint_name,
                provider=candidate.provider or "",
                model_requested=call_model,
                failover_attempt=attempt,
                error=error,
            )

        self._capture_error(
            n, last_error,
            f"Every candidate failed (tag '{tag or '(default)'}', last error: "
            f"{last_error}); the request forwards untouched.",
        )
        return None, last_error


async def _resolve_chain(db: AsyncSession, user_id: str, tag: str) -> list[Any]:
    """Ordered failover chain for one Passenger call.

    A non-empty tag resolves to the user's enabled endpoints carrying that
    role tag; an empty tag, or a tag nothing matches, falls back to the
    user's default endpoint chain — the verification per-rule -> global
    precedent. Returns ``FailoverEndpoint`` candidates with their parameter
    layers already read, so the session can close before any upstream call.
    """
    from app.services.routing import (
        _build_failover_chain,
        _endpoint_to_candidate,
        _resolve_default_endpoint,
        resolve_endpoints_by_tag,
    )

    if tag:
        tagged = await resolve_endpoints_by_tag(db, user_id, tag)
        if tagged:
            return [_endpoint_to_candidate(ep) for ep in tagged]

    primary = await _resolve_default_endpoint(db, user_id)
    if primary is None:
        return []
    return await _build_failover_chain(db, primary, user_id)


async def get_passenger_handler(
    body_json: dict[str, Any], user_id: str
) -> PassengerHandler:
    """The per-request handler, built once and stashed on ``body_json``.

    Loads the gate/budget snapshot (admin settings, user settings row) on
    first use by any cantrip position; later positions reuse it, which is
    what makes the call budget per-request rather than per-cantrip. A missing
    user_settings row means defaults (enabled), matching the lexicon
    precedent — "never opened Settings" must not silently disable anything.
    """
    cached = body_json.get(_HANDLER_KEY)
    if isinstance(cached, PassengerHandler):
        return cached

    from app.models.user_settings import UserSettings
    from app.services.admin import get_admin_settings

    admin = await get_admin_settings()
    async with async_session() as db:
        user_settings = (
            await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
        ).scalar_one_or_none()

    enabled = bool(
        admin.passenger_calling_enabled
        and (user_settings is None or user_settings.passenger_calling_enabled)
    )
    max_calls = max(0, admin.max_passenger_calls_per_request)
    user_budget = user_settings.passenger_call_budget if user_settings is not None else 0
    if user_budget and user_budget > 0:
        max_calls = min(max_calls, user_budget)

    handler = PassengerHandler(
        body_json, user_id,
        enabled=enabled,
        max_calls=max_calls,
        timeout_seconds=max(1, admin.passenger_timeout_seconds),
        passenger_parameters_json=(
            user_settings.passenger_parameters_json if user_settings is not None else "[]"
        ),
    )
    body_json[_HANDLER_KEY] = handler
    return handler
