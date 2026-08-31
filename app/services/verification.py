from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.models.verification import VerificationLog, VerificationRule
from app.services.llm_params import ParamDef, apply_to_body, parse_params, resolve
from app.services.routing import FailoverEndpoint

logger = logging.getLogger(__name__)

# Seeded defaults for the judge call. These were hard-coded literals until Phase
# 23; they are still the defaults, but a configured parameter at any layer now
# overrides them.
JUDGE_DEFAULTS: dict[str, Any] = {"max_tokens": 200, "temperature": 0.1}

VERIFICATION_SYSTEM_PROMPT = """You are a response verification system for an AI roleplay proxy.
Evaluate the given AI response against the provided rules.

Respond ONLY with a JSON object. No other text. Use this exact format:
{"violation": false, "reason": "", "severity": "none"}
{"violation": true, "reason": "Brief explanation of what rule was violated", "severity": "low|medium|high"}

Severity levels:
- none: No issue
- low: Minor issue that doesn't significantly impact quality
- medium: Noticeable problem that should be corrected
- high: Serious violation that breaks character, ignores instructions, or ruins the scene"""


@dataclass
class VerificationJudgment:
    violation: bool
    reason: str
    severity: str
    rule_name: str = ""
    raw_response: str = ""
    thinking: str = ""
    # True when this judgment is `violation=False` because the judge never ran,
    # not because the response passed. Without it a dead verification endpoint is
    # indistinguishable from a clean pass at every surface -- which is exactly
    # how the LiteLLM transport gap stayed invisible.
    errored: bool = False

    @property
    def passed(self) -> bool:
        return not self.violation


@dataclass
class VerificationCheckResult:
    approved: bool
    judgments: list[VerificationJudgment] = field(default_factory=list)
    violations: list[VerificationJudgment] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return len(self.violations) > 0

    @property
    def errors(self) -> list[VerificationJudgment]:
        """Judgments that approved by default because the judge did not run."""
        return [j for j in self.judgments if j.errored]

    @property
    def combined_reason(self) -> str:
        return "; ".join(v.reason for v in self.violations if v.reason)

    @property
    def combined_error_note(self) -> str:
        return " ".join(j.thinking for j in self.errors if j.thinking)


def judge_error_note(detail: str, rule_name: str) -> str:
    """The sentence a user reads when a rule was skipped rather than passed.

    `detail` is the HTTP status ("a 404 error") or the failure kind ("an
    exception"), phrased to slot into the sentence.
    """
    return (
        f"Verification resulted in {detail}, so rule '{rule_name}' did not process. "
        "The response was returned unchecked."
    )


@dataclass
class VerificationLoopResult:
    approved: bool
    final_content: str
    final_response_data: dict[str, Any]
    retries_used: int
    check_history: list[VerificationCheckResult]
    logs: list[VerificationLog]


def _build_verification_messages(
    content: str, rule_prompt: str, forbidden_context: str = ""
) -> list[dict[str, str]]:
    user_content = (
        f"Rules to check:\n{rule_prompt}\n\n"
        f"---\nResponse to evaluate:\n{content[:4000]}\n---\n\n"
    )
    if forbidden_context:
        user_content += f"{forbidden_context}\n\n"
    user_content += "Return JSON only."

    return [
        {"role": "system", "content": VERIFICATION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": user_content,
        },
    ]


def _parse_judgment(text: str, rule_name: str = "") -> VerificationJudgment:
    raw = text
    text = text.strip()

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        data = json.loads(text)
        return VerificationJudgment(
            violation=bool(data.get("violation", False)),
            reason=str(data.get("reason", "")),
            severity=str(data.get("severity", "none")),
            rule_name=rule_name,
            raw_response=raw,
        )
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse verification response: %s", text[:200])
        return VerificationJudgment(
            violation=False,
            reason="Verification LLM returned unparseable response",
            severity="none",
            rule_name=rule_name,
            raw_response=raw,
        )


def resolve_judge_params(
    user_layer: list[ParamDef],
    endpoint: Endpoint | None,
    model: str,
    rule: VerificationRule | None,
) -> dict[str, Any]:
    """Resolve one judge call's parameters.

    Layers, broadest first: the user's verification settings, the endpoint the
    judge will actually be called on, that endpoint's entry for the model in
    play, and finally the rule itself -- which is the closest thing to the
    message and therefore wins. This is what makes `reasoning_effort: low` on a
    cheap verification rule beat `max` on the endpoint.
    """
    from app.services.routing import endpoint_parameters, model_parameter_map

    layers: list[tuple[str, list[ParamDef]]] = [("user settings (verification)", user_layer)]
    if endpoint is not None:
        layers.append((f"endpoint '{endpoint.name}'", endpoint_parameters(endpoint)))
        layers.append((f"model '{model}'", model_parameter_map(endpoint).get(model, [])))
    if rule is not None:
        layers.append(
            (f"verification rule '{rule.name}'", parse_params(rule.parameters_json, "rule"))
        )
    return dict(resolve(layers).values)


def resolve_judge_params_for_candidate(
    user_layer: list[ParamDef],
    candidate: FailoverEndpoint,
    model: str,
    rule: VerificationRule | None,
) -> dict[str, Any]:
    """The same layers as `resolve_judge_params`, from a failover candidate.

    Candidates may differ in endpoint and model, so the endpoint and per-model
    layers have to be re-resolved for each one rather than reusing the first
    candidate's set -- the same reasoning as `_forward_with_failover` in
    `proxy.py`. `FailoverEndpoint` carries both layers already, read while the
    session was open.
    """
    layers: list[tuple[str, list[ParamDef]]] = [("user settings (verification)", user_layer)]
    label = f"endpoint '{candidate.endpoint_name}'" if candidate.endpoint_name else "endpoint"
    layers.append((label, candidate.parameters))
    layers.append((f"model '{model}'", candidate.params_for_model(model)))
    if rule is not None:
        layers.append(
            (f"verification rule '{rule.name}'", parse_params(rule.parameters_json, "rule"))
        )
    return dict(resolve(layers).values)


def _judge_candidates(
    rule: VerificationRule,
    endpoint: Endpoint,
    model: str,
    rule_endpoints: dict[str, tuple[Endpoint, str]] | None,
    rule_chains: dict[str, list[FailoverEndpoint]] | None,
) -> list[FailoverEndpoint]:
    """The ordered endpoints this rule's judge may be tried on.

    `run_verification_loop` supplies real failover chains built while the DB
    session was open. Callers without one (the ad-hoc test endpoint, and the
    tests) get a single-candidate chain from the endpoint they passed, so there
    is one code path rather than two.
    """
    from app.services.routing import _endpoint_to_candidate

    if rule_chains and rule.id in rule_chains and rule_chains[rule.id]:
        return rule_chains[rule.id]

    rule_ep, rule_model = endpoint, model
    if rule_endpoints and rule.id in rule_endpoints:
        rule_ep, rule_model = rule_endpoints[rule.id]
    if rule_ep is None:
        return []
    return [_endpoint_to_candidate(rule_ep, rule_model)]


async def check_response(
    content: str,
    rules: list[VerificationRule],
    endpoint: Endpoint,
    model: str,
    timeout: int = 120,
    forbidden_context: str = "",
    rule_endpoints: dict[str, tuple[Endpoint, str]] | None = None,
    rule_chains: dict[str, list[FailoverEndpoint]] | None = None,
    user_param_layer: list[ParamDef] | None = None,
    body_json: dict[str, Any] | None = None,
) -> VerificationCheckResult:
    """Judge `content` against each rule, one upstream call per rule.

    Every call goes through `proxy._do_forward`, so a judge on a provider
    endpoint routes through LiteLLM exactly as the driver does. Each rule walks
    its own failover chain; a candidate that does not answer with 200 advances to
    the next, and only an exhausted chain produces an errored judgment.

    A non-200 has always meant "no violation, continue" -- an unreachable judge
    must not block the user's response. That is preserved, but the judgment is
    now marked `errored` and carries a note saying the rule did not run.
    """
    from app.services.debug_metrics import PURPOSE_VERIFICATION, record_llm_call
    from app.services.proxy import _build_upstream_url, _do_forward

    judgments: list[VerificationJudgment] = []
    violations: list[VerificationJudgment] = []
    httpx_timeout = httpx.Timeout(timeout, connect=15.0)

    if not rules and forbidden_context:
        judgment = VerificationJudgment(
            violation=True,
            reason="Forbidden words/phrases detected in response",
            severity="high",
            rule_name="forbidden_words",
        )
        judgments.append(judgment)
        violations.append(judgment)
        return VerificationCheckResult(
            approved=False, judgments=judgments, violations=violations
        )

    for rule in rules:
        candidates = _judge_candidates(rule, endpoint, model, rule_endpoints, rule_chains)
        if not candidates:
            judgments.append(
                VerificationJudgment(
                    violation=False,
                    reason="No verification endpoint available",
                    severity="none",
                    rule_name=rule.name,
                    thinking=judge_error_note("no available endpoint", rule.name),
                    errored=True,
                )
            )
            continue

        messages = _build_verification_messages(content, rule.prompt, forbidden_context)
        judged = False
        last_detail = "an unknown error"

        for attempt, candidate in enumerate(candidates):
            judge_model = candidate.model or model or "gpt-4"
            params = resolve_judge_params_for_candidate(
                user_param_layer or [], candidate, judge_model, rule
            )

            body: dict[str, Any] = {
                "model": judge_model,
                "messages": messages,
                **JUDGE_DEFAULTS,
                "stream": False,
            }
            body = apply_to_body(body, params)

            url = _build_upstream_url(
                candidate.base_url, "/v1/chat/completions", candidate.api_base_path
            )
            headers = {
                "Authorization": f"Bearer {candidate.api_key}",
                "Content-Type": "application/json",
            }

            started = time.monotonic()
            try:
                data, status_code = await _do_forward(
                    "POST", url, headers, json.dumps(body).encode(), httpx_timeout,
                    provider=candidate.provider,
                    base_url=candidate.base_url,
                    api_key=candidate.api_key,
                    configured=params,
                )
            except Exception as exc:
                logger.exception("Verification check failed for rule '%s'", rule.name)
                record_llm_call(
                    body_json or {},
                    purpose=PURPOSE_VERIFICATION,
                    latency_ms=(time.monotonic() - started) * 1000.0,
                    status_code=0,
                    response_data=None,
                    messages=messages,
                    endpoint_id=candidate.endpoint_id,
                    endpoint_name=candidate.endpoint_name,
                    provider=candidate.provider,
                    model_requested=judge_model,
                    failover_attempt=attempt,
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                last_detail = "an exception"
                continue

            record_llm_call(
                body_json or {},
                purpose=PURPOSE_VERIFICATION,
                latency_ms=(time.monotonic() - started) * 1000.0,
                status_code=status_code,
                response_data=data,
                messages=messages,
                endpoint_id=candidate.endpoint_id,
                endpoint_name=candidate.endpoint_name,
                provider=candidate.provider,
                model_requested=judge_model,
                failover_attempt=attempt,
            )

            if status_code != 200:
                logger.warning(
                    "Verification LLM returned %d for rule '%s' on endpoint '%s': %s",
                    status_code, rule.name, candidate.endpoint_name, str(data)[:200],
                )
                last_detail = f"a {status_code} error"
                continue

            message = data.get("choices", [{}])[0].get("message", {})
            llm_content = message.get("content", "")
            thinking = message.get("reasoning_content", "") or message.get("thinking", "")

            judgment = _parse_judgment(llm_content, rule.name)
            judgment.thinking = thinking
            judgments.append(judgment)
            if judgment.violation:
                violations.append(judgment)
            judged = True
            break

        if not judged:
            judgments.append(
                VerificationJudgment(
                    violation=False,
                    reason=f"Verification endpoint error ({last_detail})",
                    severity="none",
                    rule_name=rule.name,
                    thinking=judge_error_note(last_detail, rule.name),
                    errored=True,
                )
            )

    return VerificationCheckResult(
        approved=len(violations) == 0,
        judgments=judgments,
        violations=violations,
    )


def _apply_resubmission_strategy(
    body_json: dict[str, Any],
    violations: list[VerificationJudgment],
    strategy: str,
) -> dict[str, Any]:
    # Deep-copy only the upstream-facing keys; _gitv_* internals (including the
    # failover chain, which holds non-serializable FailoverEndpoint objects) are
    # excluded so the retry body is clean JSON.
    result = json.loads(json.dumps(
        {k: v for k, v in body_json.items() if not k.startswith("_gitv")}
    ))
    combined_reason = "; ".join(v.reason for v in violations if v.reason)

    if strategy == "rewrite":
        last_assistant_content = ""
        for msg in reversed(result.get("messages", [])):
            if msg.get("role") == "assistant":
                last_assistant_content = msg.get("content", "")
                break

        if last_assistant_content:
            result.setdefault("messages", []).append(
                {"role": "assistant", "content": last_assistant_content}
            )
        result["messages"].append(
            {
                "role": "user",
                "content": (
                    f"[Correction Required] The previous response had issues: "
                    f"{combined_reason}\nPlease rewrite the response fixing these problems."
                ),
            }
        )
    else:
        result.setdefault("messages", []).append(
            {
                "role": "system",
                "content": (
                    f"[Verification Correction] The following issues were detected "
                    f"in the previous generation: {combined_reason}\n"
                    f"Regenerate the response addressing these issues. "
                    f"Do not repeat the same mistakes."
                ),
            }
        )

    return result


async def _create_log_entry(
    db: AsyncSession,
    user_id: str,
    rule_name: str,
    conversation_id: str,
    response_snippet: str,
    check_result: VerificationCheckResult,
    retries_used: int,
    approved: bool,
) -> VerificationLog:
    # A check that errored approved by default, so it has no violation reason. It
    # used to write a blank one, which read on the Logs page as a clean pass --
    # the note goes in the same column so a skipped rule is visible there.
    reason = check_result.combined_reason
    if check_result.errors:
        note = check_result.combined_error_note
        reason = f"{reason} {note}".strip() if reason else note

    log = VerificationLog(
        user_id=user_id,
        rule_name=rule_name,
        conversation_id=conversation_id,
        response_snippet=response_snippet[:500],
        violation_detected=check_result.has_violations,
        violation_reason=reason,
        severity=check_result.violations[0].severity if check_result.violations else "none",
        retries_used=retries_used,
        approved=approved,
    )
    db.add(log)
    await db.commit()
    return log


async def _load_verification_param_layer(db: AsyncSession, user_id: str) -> list[ParamDef]:
    """The user's verification-role parameter layer."""
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user_id))
    s = result.scalar_one_or_none()
    if s is None:
        return []
    return parse_params(s.verification_parameters_json, "user settings (verification)")


async def load_verification_config(
    db: AsyncSession, user_id: str, tags: list | None = None
) -> tuple[list[VerificationRule], Endpoint | None, str, int]:
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    user_settings = settings_result.scalar_one_or_none()

    if not user_settings or not user_settings.verification_enabled:
        return [], None, "", 0

    rules_result = await db.execute(
        select(VerificationRule)
        .where(VerificationRule.user_id == user_id)
        .order_by(VerificationRule.execution_order, VerificationRule.created_at)
    )
    candidate_rules = list(rules_result.scalars().all())

    from app.services.tagging import should_activate_resource
    rules = [
        r for r in candidate_rules
        if should_activate_resource(r.tag, "verify", r.is_active, False, r.user_id, user_id, tags or [])
    ]

    if not rules:
        return [], None, "", 0

    endpoint = None
    if user_settings.verification_endpoint_id:
        ep_result = await db.execute(
            select(Endpoint).where(
                Endpoint.id == user_settings.verification_endpoint_id,
                Endpoint.enabled.is_(True),
            )
        )
        endpoint = ep_result.scalar_one_or_none()

    if endpoint is None:
        return [], None, "", 0

    max_retries = max(r.max_retries for r in rules)

    from app.services.admin import get_caps
    caps = await get_caps()
    max_retries = min(max_retries, caps["max_verification_retries"])

    return rules, endpoint, user_settings.verification_model, max_retries


async def is_verification_enabled(user_id: str, tags: list | None = None) -> bool:
    async with async_session() as db:
        rules, endpoint, _, _ = await load_verification_config(db, user_id, tags)
        return len(rules) > 0 and endpoint is not None


async def run_verification_loop(
    response_data: dict[str, Any],
    body_json: dict[str, Any],
    method: str,
    url: str,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    user_id: str,
    conversation_id: str = "",
    tags: list | None = None,
    path: str = "",
) -> tuple[dict[str, Any], VerificationLoopResult | None]:
    from app.services.routing import _build_failover_chain, _endpoint_to_candidate

    async with async_session() as db:
        rules, v_endpoint, v_model, max_retries = await load_verification_config(
            db, user_id, tags
        )

        user_verification_layer = await _load_verification_param_layer(db, user_id)

        rule_endpoints: dict[str, tuple[Endpoint, str]] = {}
        # A judge gets the same failover treatment as the driver and map stages:
        # the configured endpoint first, then its tag-mates. Built here because
        # `_build_failover_chain` needs the session, and every caller closes it
        # before any upstream call.
        rule_chains: dict[str, list[FailoverEndpoint]] = {}
        chain_cache: dict[str, list[FailoverEndpoint]] = {}
        for rule in rules:
            if rule.verification_endpoint_id:
                ep_result = await db.execute(
                    select(Endpoint).where(
                        Endpoint.id == rule.verification_endpoint_id,
                        Endpoint.enabled.is_(True),
                    )
                )
                rule_ep = ep_result.scalar_one_or_none()
                if rule_ep:
                    rule_endpoints[rule.id] = (rule_ep, rule.verification_model or v_model)
            elif rule.verification_model and rule.verification_model != v_model:
                rule_endpoints[rule.id] = (v_endpoint, rule.verification_model)

            judge_ep, judge_model = rule_endpoints.get(rule.id, (v_endpoint, v_model))
            if judge_ep is None:
                continue
            if judge_ep.id not in chain_cache:
                chain_cache[judge_ep.id] = await _build_failover_chain(db, judge_ep, user_id)
            # The chain's own candidates carry each endpoint's default model; the
            # judge model the user configured wins on the primary, and a fallback
            # keeps whatever model it was configured with.
            chain = list(chain_cache[judge_ep.id])
            if chain:
                chain[0] = _endpoint_to_candidate(judge_ep, judge_model)
            rule_chains[rule.id] = chain

    forbidden_summary = response_data.pop("_gitv_forbidden_summary", "")

    if not rules and not forbidden_summary:
        if forbidden_summary:
            response_data["_gitv_forbidden_summary"] = forbidden_summary
        return response_data, None

    if not v_endpoint:
        if forbidden_summary:
            response_data["_gitv_forbidden_summary"] = forbidden_summary
        return response_data, None

    strategy = rules[0].resubmission_strategy if rules else "add_instructions"

    check_history: list[VerificationCheckResult] = []
    logs: list[VerificationLog] = []
    retries = 0
    current_data = response_data
    current_content = _extract_content(current_data)

    while True:
        check_result = await check_response(
            current_content, rules, v_endpoint, v_model,
            forbidden_context=forbidden_summary,
            rule_endpoints=rule_endpoints if rule_endpoints else None,
            rule_chains=rule_chains if rule_chains else None,
            user_param_layer=user_verification_layer,
            body_json=body_json,
        )
        check_history.append(check_result)

        if check_result.errors:
            logger.warning(
                "Verification: %d of %d rule(s) did not run -- %s",
                len(check_result.errors), len(check_result.judgments),
                check_result.combined_error_note,
            )

        logger.info(
            "Verification check %d: approved=%s, violations=%d",
            retries + 1,
            check_result.approved,
            len(check_result.violations),
        )

        if check_result.approved:
            async with async_session() as db:
                log = await _create_log_entry(
                    db, user_id, rules[0].name, conversation_id,
                    current_content, check_result, retries, approved=True,
                )
                logs.append(log)
            break

        if retries >= max_retries:
            async with async_session() as db:
                log = await _create_log_entry(
                    db, user_id, rules[0].name, conversation_id,
                    current_content, check_result, retries, approved=False,
                )
                logs.append(log)
            logger.warning(
                "Verification: max retries (%d) exceeded, returning unapproved response",
                max_retries,
            )
            break

        async with async_session() as db:
            log = await _create_log_entry(
                db, user_id, rules[0].name, conversation_id,
                current_content, check_result, retries, approved=False,
            )
            logs.append(log)

        retry_body = _apply_resubmission_strategy(
            body_json, check_result.violations, strategy
        )
        retry_body["stream"] = False
        retry_body_bytes = json.dumps(retry_body).encode()

        logger.info("Verification: retrying request (attempt %d)", retries + 1)
        # The retry re-sends the user's own request, so it takes the driver's
        # transport and the driver's failover chain -- including LiteLLM when the
        # endpoint has a provider. Sending it raw here was the same gap the judge
        # had, one layer up.
        from app.services.proxy import _do_forward, _forward_with_failover

        driver_chain = body_json.get("_gitv_failover_chain") or []
        try:
            if driver_chain and path:
                retry_data, retry_status = await _forward_with_failover(
                    body_json, driver_chain, method, path, headers, timeout, retry_body_bytes,
                )
            else:
                retry_data, retry_status = await _do_forward(
                    method, url, headers, retry_body_bytes, timeout,
                    provider=body_json.get("_gitv_provider", ""),
                    base_url=body_json.get("_gitv_base_url", ""),
                    api_key=body_json.get("_gitv_api_key", ""),
                    configured=body_json.get("_gitv_llm_params"),
                )
        except Exception:
            logger.exception("Verification retry request failed")
            break

        # A failed retry keeps the last good response, exactly as before -- the
        # error body must never become what the user is handed.
        if retry_status != 200:
            logger.warning("Verification retry returned status %d", retry_status)
            break

        current_data = retry_data
        current_content = _extract_content(current_data)

        retries += 1

    loop_result = VerificationLoopResult(
        approved=check_history[-1].approved if check_history else True,
        final_content=current_content,
        final_response_data=current_data,
        retries_used=retries,
        check_history=check_history,
        logs=logs,
    )

    return current_data, loop_result


def _extract_content(response_data: dict[str, Any]) -> str:
    choices = response_data.get("choices", [])
    if not choices:
        return ""
    return choices[0].get("message", {}).get("content", "")
