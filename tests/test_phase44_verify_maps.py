"""Tests for Phase 44: Hybrid Verification Regex Pre-Check & Map Final-Output Navigator Pass."""
from __future__ import annotations

import json
import logging
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app.services.map_pipeline as map_pipeline_mod
from app.models.endpoint import Endpoint
from app.models.map import Map, MapStage
from app.models.user_settings import UserSettings
from app.models.verification import VerificationLog, VerificationRule
from app.services.map_pipeline import run_map_pipeline
from app.services.verification import check_response


def _mock_endpoint(base_url: str = "https://verify.test", api_key: str = "sk-verify") -> Endpoint:
    return Endpoint(
        id="ep-verify-1",
        user_id="user-1",
        name="Verify Endpoint",
        base_url=base_url,
        api_key=api_key,
        api_base_path="/api",
        enabled=True,
    )


def _mock_rule(
    name: str = "No AI Slop",
    prompt: str = "Do not break character",
    regex_pattern: str = "",
) -> VerificationRule:
    rule = VerificationRule(
        id="rule-1",
        user_id="user-1",
        name=name,
        description="Test rule",
        prompt=prompt,
        is_active=True,
        max_retries=2,
        execution_order=10,
        resubmission_strategy="add_instructions",
    )
    setattr(rule, "regex_pattern", regex_pattern)
    return rule


def _forward_returning(*results):
    calls: list[dict] = []
    queue = list(results)

    async def fake(method, url, headers, body, timeout, **kwargs):
        calls.append({"url": url, "body": json.loads(body)})
        return queue.pop(0) if queue else ({}, 500)

    return patch("app.services.proxy._do_forward", side_effect=fake), calls


class TestHybridVerificationRegexPrecheck:
    @pytest.mark.asyncio
    async def test_regex_match_short_circuits_without_judge_call(self):
        rule = _mock_rule(regex_pattern=r"\b(as an AI|I cannot)\b")
        other_rule = VerificationRule(
            id="rule-2",
            user_id="user-1",
            name="Tone Rule",
            description="Check tone",
            prompt="Check tone",
            is_active=True,
            max_retries=2,
            execution_order=20,
            resubmission_strategy="add_instructions",
        )
        endpoint = _mock_endpoint()
        body_json = {"_gitv_debug": {"stages": []}}

        patcher, calls = _forward_returning()
        with patcher:
            result = await check_response(
                "Sorry, as an AI I cannot fulfill that request.",
                [other_rule, rule],
                endpoint,
                "judge-model",
                body_json=body_json,
            )

        assert len(calls) == 0, "Regex match must skip Navigator LLM judge completely"
        assert result.approved is False
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.passed is False
        assert v.severity == "high"
        assert v.rule_name == "No AI Slop"
        assert v.reason == r"Regex pre-check matched: \b(as an AI|I cannot)\b"

        stages = body_json["_gitv_debug"]["stages"]
        assert len(stages) == 1
        assert stages[0]["name"] == "verification_regex_precheck"
        assert stages[0]["metadata"]["matched_rules"] == ["No AI Slop"]

    @pytest.mark.asyncio
    async def test_non_matching_regex_falls_through_to_llm_judge(self):
        rule = _mock_rule(regex_pattern=r"\b(as an AI|I cannot)\b")
        endpoint = _mock_endpoint()
        judge_reply = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"violation": False, "reason": "", "severity": "none"}
                        ),
                    }
                }
            ]
        }

        patcher, calls = _forward_returning((judge_reply, 200))
        with patcher:
            result = await check_response(
                "The knight drew his sword and stepped into the tavern.",
                [rule],
                endpoint,
                "judge-model",
            )

        assert len(calls) == 1
        assert result.approved is True
        assert result.violations == []
        assert len(result.judgments) == 1
        assert result.judgments[0].passed is True

    @pytest.mark.asyncio
    async def test_invalid_regex_degrades_gracefully(self, caplog):
        rule = _mock_rule(regex_pattern="[unclosed(")
        endpoint = _mock_endpoint()
        judge_reply = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"violation": False, "reason": "", "severity": "none"}
                        ),
                    }
                }
            ]
        }

        patcher, calls = _forward_returning((judge_reply, 200))
        with caplog.at_level(logging.WARNING):
            with patcher:
                result = await check_response(
                    "Normal response text.",
                    [rule],
                    endpoint,
                    "judge-model",
                )

        assert len(calls) == 1
        assert result.approved is True
        assert any("Invalid regex pattern" in rec.message for rec in caplog.records)


class TestMapFinalOutputVerification:
    async def _seed_map_and_verification(self, client, *, final_enabled: bool):
        client_http, _token, _api_key = client
        me_resp = await client_http.get("/api/auth/me")
        assert me_resp.status_code == 200
        user_id = me_resp.json()["id"]

        async with map_pipeline_mod.async_session() as db:
            driver_ep = Endpoint(
                id="ep-map-driver",
                user_id=user_id,
                name="Map Driver",
                base_url="http://driver.test",
                api_key="sk-driver",
                enabled=True,
            )
            judge_ep = Endpoint(
                id="ep-map-judge",
                user_id=user_id,
                name="Map Judge",
                base_url="http://judge.test",
                api_key="sk-judge",
                enabled=True,
            )
            db.add_all([driver_ep, judge_ep])

            us_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user_id)
            )
            us = us_result.scalar_one_or_none()
            if us is None:
                us = UserSettings(user_id=user_id)
                db.add(us)
            us.verification_enabled = True
            us.verification_endpoint_id = judge_ep.id
            us.verification_model = "judge-model"

            rule = VerificationRule(
                id="rule-final-1",
                user_id=user_id,
                name="Final Quality Rule",
                description="Must stay in character",
                prompt="Check character consistency",
                is_active=True,
                max_retries=2,
                execution_order=1,
                resubmission_strategy="add_instructions",
            )
            db.add(rule)

            map_row = Map(
                id="map-final-1",
                user_id=user_id,
                name="FinalVerifyMap",
                is_active=True,
                final_verification_enabled=final_enabled,
            )
            stage = MapStage(
                id="stage-final-1",
                map_id=map_row.id,
                stage_order=1,
                name="Draft",
                system_instructions="Write a reply.",
                endpoint_id=driver_ep.id,
                output_mode="persist",
                verification_enabled=False,
            )
            db.add_all([map_row, stage])
            await db.commit()

            loaded_map = (
                await db.execute(
                    select(Map)
                    .where(Map.id == map_row.id)
                    .options(selectinload(Map.stages).selectinload(MapStage.resources))
                )
            ).scalar_one()

        return user_id, loaded_map

    @pytest.mark.asyncio
    async def test_final_verification_off_by_default(self, admin_client):
        user_id, map_ns = await self._seed_map_and_verification(
            admin_client, final_enabled=False
        )
        driver_reply = {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "Drafted reply."}}],
        }
        body_json = {
            "model": "driver-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "_gitv_debug": {"stages": []},
        }

        patcher, calls = _forward_returning((driver_reply, 200))
        with patcher:
            out = await run_map_pipeline(
                body_json, user_id, {}, map_ns, httpx.Timeout(30.0)
            )

        assert out["choices"][0]["message"]["content"] == "Drafted reply."
        assert len(calls) == 1
        assert "_gitv_map_vresult" not in body_json
        assert all(
            s["name"] != "map_final_verification"
            for s in body_json["_gitv_debug"]["stages"]
        )

    @pytest.mark.asyncio
    async def test_final_verification_enabled_runs_report_only_once(self, admin_client):
        user_id, map_ns = await self._seed_map_and_verification(
            admin_client, final_enabled=True
        )
        driver_reply = {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "OOC reply."}}],
        }
        judge_reply = {
            "id": "judge-1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "violation": True,
                                "reason": "Broke character",
                                "severity": "high",
                            }
                        ),
                    }
                }
            ],
        }
        body_json = {
            "model": "driver-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "_gitv_debug": {"stages": []},
        }

        patcher, calls = _forward_returning((driver_reply, 200), (judge_reply, 200))
        with patcher:
            out = await run_map_pipeline(
                body_json, user_id, {}, map_ns, httpx.Timeout(30.0)
            )

        # Exactly 2 calls: 1 driver stage + 1 final judge (never re-runs the map)
        assert len(calls) == 2
        assert out["choices"][0]["message"]["content"] == "OOC reply."

        vresult = body_json.get("_gitv_map_vresult")
        assert vresult is not None
        assert vresult.approved is False
        assert vresult.retries_used == 0
        assert len(vresult.check_history) == 1
        check = vresult.check_history[0]
        assert check.approved is False
        assert len(check.judgments) == 1
        assert check.judgments[0].rule_name == "Final Quality Rule"
        assert check.judgments[0].reason == "Broke character"

        debug_stages = [
            s for s in body_json["_gitv_debug"]["stages"]
            if s["name"] == "map_final_verification"
        ]
        assert len(debug_stages) == 1
        assert debug_stages[0]["metadata"]["report_only"] is True
        assert debug_stages[0]["metadata"]["approved"] is False

        async with map_pipeline_mod.async_session() as db:
            logs = (
                await db.execute(
                    select(VerificationLog).where(VerificationLog.user_id == user_id)
                )
            ).scalars().all()
            assert len(logs) == 1
            assert logs[0].approved is False
            assert logs[0].violation_detected is True
            assert logs[0].retries_used == 0
            assert "Broke character" in logs[0].violation_reason

    @pytest.mark.asyncio
    async def test_final_verification_respects_verify_off_override(self, admin_client):
        user_id, map_ns = await self._seed_map_and_verification(
            admin_client, final_enabled=True
        )
        driver_reply = {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "Reply."}}],
        }
        body_json = {
            "model": "driver-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "_gitv_command_overrides": {"verify": False},
            "_gitv_debug": {"stages": []},
        }

        patcher, calls = _forward_returning((driver_reply, 200))
        with patcher:
            await run_map_pipeline(
                body_json, user_id, {}, map_ns, httpx.Timeout(30.0)
            )

        assert len(calls) == 1
        assert "_gitv_map_vresult" not in body_json

    @pytest.mark.asyncio
    async def test_final_verification_logs_conversation_id_from_header(self, admin_client):
        """The final pass logs under the client's conversation header -- the
        same identity the main verification loop uses (the old body key was
        never written anywhere, so rows always logged conversation_id='')."""
        user_id, map_ns = await self._seed_map_and_verification(
            admin_client, final_enabled=True
        )
        driver_reply = {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "Clean reply."}}],
        }
        judge_reply = {
            "id": "judge-1",
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"violation": False, "reason": ""}),
                }
            }],
        }
        body_json = {
            "model": "driver-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "_gitv_debug": {"stages": []},
        }

        patcher, _calls = _forward_returning((driver_reply, 200), (judge_reply, 200))
        with patcher:
            out = await run_map_pipeline(
                body_json, user_id, {"x-conversation-id": "conv-map-1"},
                map_ns, httpx.Timeout(30.0),
            )

        assert out["choices"][0]["message"]["content"] == "Clean reply."
        async with map_pipeline_mod.async_session() as db:
            logs = (
                await db.execute(
                    select(VerificationLog).where(VerificationLog.user_id == user_id)
                )
            ).scalars().all()
            assert len(logs) == 1
            assert logs[0].conversation_id == "conv-map-1"
            assert logs[0].approved is True

    @pytest.mark.asyncio
    async def test_final_verification_failure_degrades_not_fails_request(self, admin_client):
        """Report-only must degrade, never 500: every stage has already been
        paid for when the final pass runs."""
        user_id, map_ns = await self._seed_map_and_verification(
            admin_client, final_enabled=True
        )
        driver_reply = {
            "id": "resp-1",
            "choices": [{"message": {"role": "assistant", "content": "Paid output."}}],
        }
        body_json = {
            "model": "driver-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "_gitv_debug": {"stages": []},
        }

        patcher, calls = _forward_returning((driver_reply, 200))
        with (
            patcher,
            patch(
                "app.services.map_pipeline._run_final_map_verification",
                side_effect=RuntimeError("simulated DB failure"),
            ),
        ):
            out = await run_map_pipeline(
                body_json, user_id, {}, map_ns, httpx.Timeout(30.0)
            )

        # The map's paid-for output is returned unverified, not a 500.
        assert out["choices"][0]["message"]["content"] == "Paid output."
        assert len(calls) == 1
        assert "_gitv_map_vresult" not in body_json
