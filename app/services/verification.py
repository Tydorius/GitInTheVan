from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.endpoint import Endpoint
from app.models.user_settings import UserSettings
from app.models.verification import VerificationLog, VerificationRule

logger = logging.getLogger(__name__)

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
    def combined_reason(self) -> str:
        return "; ".join(v.reason for v in self.violations if v.reason)


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
        )
    except (json.JSONDecodeError, TypeError):
        logger.warning("Could not parse verification response: %s", text[:200])
        return VerificationJudgment(
            violation=False,
            reason="Verification LLM returned unparseable response",
            severity="none",
            rule_name=rule_name,
        )


async def check_response(
    content: str,
    rules: list[VerificationRule],
    endpoint: Endpoint,
    model: str,
    timeout: int = 120,
    forbidden_context: str = "",
    rule_endpoints: dict[str, tuple[Endpoint, str]] | None = None,
) -> VerificationCheckResult:
    judgments: list[VerificationJudgment] = []
    violations: list[VerificationJudgment] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=15.0)
    ) as client:
        if not rules and forbidden_context:
            judgment = VerificationJudgment(
                violation=True,
                reason="Forbidden words/phrases detected in response",
                severity="high",
                rule_name="forbidden_words",
            )
            judgments.append(judgment)
            violations.append(judgment)
        else:
            for rule in rules:
                rule_ep = endpoint
                rule_model = model
                if rule_endpoints and rule.id in rule_endpoints:
                    rule_ep, rule_model = rule_endpoints[rule.id]

                messages = _build_verification_messages(content, rule.prompt, forbidden_context)
                body: dict[str, Any] = {
                    "model": rule_model or "gpt-4",
                    "messages": messages,
                    "max_tokens": 200,
                    "temperature": 0.1,
                    "stream": False,
                }

                headers = {
                    "Authorization": f"Bearer {rule_ep.api_key}",
                    "Content-Type": "application/json",
                }
                api_base_path = rule_ep.api_base_path or ""
                if api_base_path.endswith("/chat/completions"):
                    url = f"{rule_ep.base_url}{api_base_path}"
                else:
                    path_prefix = api_base_path or "/v1"
                    url = f"{rule_ep.base_url}{path_prefix}/chat/completions"

                try:
                    resp = await client.post(url, json=body, headers=headers)
                    if resp.status_code != 200:
                        logger.warning(
                            "Verification LLM returned %d for rule '%s': %s",
                            resp.status_code,
                            rule.name,
                            resp.text[:200],
                        )
                        judgments.append(
                            VerificationJudgment(
                                violation=False,
                                reason=f"Verification endpoint error ({resp.status_code})",
                                severity="none",
                                rule_name=rule.name,
                            )
                        )
                        continue

                    data = resp.json()
                    llm_content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    )

                    judgment = _parse_judgment(llm_content, rule.name)
                    judgments.append(judgment)
                    if judgment.violation:
                        violations.append(judgment)

                except Exception:
                    logger.exception("Verification check failed for rule '%s'", rule.name)
                    judgments.append(
                        VerificationJudgment(
                            violation=False,
                            reason="Verification check failed with exception",
                            severity="none",
                            rule_name=rule.name,
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
    result = json.loads(json.dumps(body_json))
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
    log = VerificationLog(
        user_id=user_id,
        rule_name=rule_name,
        conversation_id=conversation_id,
        response_snippet=response_snippet[:500],
        violation_detected=check_result.has_violations,
        violation_reason=check_result.combined_reason,
        severity=check_result.violations[0].severity if check_result.violations else "none",
        retries_used=retries_used,
        approved=approved,
    )
    db.add(log)
    await db.commit()
    return log


async def load_verification_config(
    db: AsyncSession, user_id: str
) -> tuple[list[VerificationRule], Endpoint | None, str, int]:
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    user_settings = settings_result.scalar_one_or_none()

    if not user_settings or not user_settings.verification_enabled:
        return [], None, "", 0

    rules_result = await db.execute(
        select(VerificationRule)
        .where(
            VerificationRule.user_id == user_id,
            VerificationRule.is_active.is_(True),
        )
        .order_by(VerificationRule.execution_order, VerificationRule.created_at)
    )
    rules = list(rules_result.scalars().all())

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

    return rules, endpoint, user_settings.verification_model, max_retries


async def is_verification_enabled(user_id: str) -> bool:
    async with async_session() as db:
        rules, endpoint, _, _ = await load_verification_config(db, user_id)
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
) -> tuple[dict[str, Any], VerificationLoopResult | None]:
    async with async_session() as db:
        rules, v_endpoint, v_model, max_retries = await load_verification_config(
            db, user_id
        )

        rule_endpoints: dict[str, tuple[Endpoint, str]] = {}
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
        )
        check_history.append(check_result)

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
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                retry_resp = await client.request(
                    method, url, headers=headers, content=retry_body_bytes
                )
                if retry_resp.status_code == 200:
                    try:
                        current_data = retry_resp.json()
                    except Exception:
                        logger.warning("Verification retry returned non-JSON, treating as text")
                        current_data = {
                            "choices": [{"message": {"content": retry_resp.text[:4000]}}]
                        }
                    current_content = _extract_content(current_data)
                else:
                    logger.warning(
                        "Verification retry returned status %d", retry_resp.status_code
                    )
                    break
            except Exception:
                logger.exception("Verification retry request failed")
                break

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
