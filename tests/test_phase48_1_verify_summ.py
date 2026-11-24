"""Tests for 0.48.1 (Phase B): regex-only verification without a judge endpoint,
summarization degradation / empty-delta hardening, and thinking_level validation."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.services.llm_params import ParameterDef, validate_params
from app.services.summarization import compute_boundary_hash, maybe_summarize
from app.services.verification import check_response


class TestRegexOnlyVerificationWithoutJudge:
    @pytest.mark.asyncio
    async def test_regex_rule_passes_cleanly_without_judge_endpoint(self):
        rule = SimpleNamespace(
            id="r1",
            name="No Slop",
            prompt="Reject slop",
            regex_pattern=r"\bshivers down (?:his|her|their) spine\b",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("The knight drew his sword quietly.", [rule], None, "")
        assert res.approved is True
        assert res.has_violations is False
        assert res.errors == []
        assert len(res.judgments) == 1
        assert res.judgments[0].errored is False
        assert "Regex pre-check passed" in res.judgments[0].reason

    @pytest.mark.asyncio
    async def test_regex_rule_rejects_without_judge_endpoint(self):
        rule = SimpleNamespace(
            id="r1",
            name="No Slop",
            prompt="Reject slop",
            regex_pattern=r"\bshivers down (?:his|her|their) spine\b",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("It sent shivers down his spine.", [rule], None, "")
        assert res.approved is False
        assert res.has_violations is True
        assert len(res.violations) == 1
        assert "Regex pre-check matched" in res.violations[0].reason

    @pytest.mark.asyncio
    async def test_non_regex_rule_without_judge_endpoint_still_records_error(self):
        rule = SimpleNamespace(
            id="r2",
            name="LLM Only Rule",
            prompt="Check tone",
            regex_pattern="",
            verification_endpoint_id=None,
            verification_model="",
            parameters_json="[]",
        )
        res = await check_response("Some text.", [rule], None, "")
        assert res.approved is True
        assert len(res.errors) == 1
        assert res.errors[0].errored is True


class TestRegexOnlyVerificationGates:
    """The gates that route a request into check_response.

    The tests above call check_response directly with endpoint=None, so they
    stay green even if load_verification_config / is_verification_enabled keep
    answering "verification off" for a regex-only user, which would leave the
    feature dead in the real pipeline. These go through the gates and the proxy
    with nothing mocked below the upstream HTTP boundary.
    """

    DRIVER_URL = "http://mock-backend:9999"
    PATTERN = r"\bshivers down (?:his|her|their) spine\b"
    SLOP = "It sent shivers down his spine."
    CLEAN = "The knight drew his sword quietly."

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings

        monkeypatch.setattr(
            "app.services.proxy.settings",
            Settings(
                default_endpoint_url=self.DRIVER_URL,
                default_endpoint_api_key="sk-test-key",
                default_endpoint_model="test-model",
            ),
        )

    async def _setup(self, client, regex_pattern):
        """Driver endpoint, verification on with NO judge endpoint, one active rule."""
        driver_ep = await client.post("/api/endpoints", json={
            "name": "Driver", "base_url": self.DRIVER_URL, "api_key": "sk-driver",
        })
        await client.put("/api/settings", json={"default_endpoint_id": driver_ep.json()["id"]})
        rule = await client.post("/api/verification/rules", json={
            "name": "No Slop", "prompt": "Reject slop.", "regex_pattern": regex_pattern,
            "max_retries": 1, "is_active": True,
        })
        assert rule.status_code == 201, rule.text
        rule_id = rule.json()["id"]
        settings = await client.put("/api/verification/settings", json={"verification_enabled": True})
        assert settings.status_code == 200, settings.text
        # The precondition the whole feature rests on.
        assert settings.json()["verification_endpoint_id"] is None
        return rule_id

    @staticmethod
    async def _admin_id() -> str:
        from sqlalchemy import select

        from app.models.user import User
        from tests.conftest import TestSessionLocal

        async with TestSessionLocal() as db:
            return (await db.execute(select(User.id).where(User.username == "admin"))).scalar_one()

    def _driver_answers(self, httpx_mock, *replies: str) -> None:
        for text in replies:
            httpx_mock.add_response(
                url=f"{self.DRIVER_URL}/v1/chat/completions",
                json={"id": "resp-1", "choices": [
                    {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"},
                ]},
                status_code=200,
            )

    @pytest.mark.asyncio
    async def test_gates_open_for_a_regex_only_rule_without_a_judge(self, admin_client):
        from app.services.verification import is_verification_enabled, load_verification_config
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        await self._setup(client, self.PATTERN)
        user_id = await self._admin_id()

        assert await is_verification_enabled(user_id) is True

        async with TestSessionLocal() as db:
            rules, endpoint, _, max_retries = await load_verification_config(db, user_id)
        assert [r.name for r in rules] == ["No Slop"]
        assert endpoint is None
        assert max_retries == 1

    @pytest.mark.asyncio
    async def test_gates_stay_closed_for_a_judge_only_rule_without_a_judge(self, admin_client):
        """Exclusion: nothing deterministic to run and no endpoint to judge on."""
        from app.services.verification import is_verification_enabled, load_verification_config
        from tests.conftest import TestSessionLocal

        client, _, _ = admin_client
        await self._setup(client, "")
        user_id = await self._admin_id()

        assert await is_verification_enabled(user_id) is False

        async with TestSessionLocal() as db:
            assert await load_verification_config(db, user_id) == ([], None, "", 0)

    @pytest.mark.asyncio
    async def test_regex_rejection_resubmits_the_driver_with_no_judge_call(
        self, admin_client, httpx_mock
    ):
        client, _, api_key = admin_client
        await self._setup(client, self.PATTERN)
        self._driver_answers(httpx_mock, self.SLOP, self.CLEAN)

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Hello."}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == self.CLEAN
        # The rejection was acted on (one resubmission), and the driver is the
        # only upstream called: no judge endpoint exists to call.
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert urls == [f"{self.DRIVER_URL}/v1/chat/completions"] * 2

    @staticmethod
    def _streamed_text(resp) -> str:
        """Content re-assembled from the simulated SSE stream, chunking-independent."""
        parts = []
        for line in resp.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                parts.append(json.loads(line[len("data: "):])["choices"][0]["delta"].get("content", ""))
        return "".join(parts)

    @pytest.mark.asyncio
    async def test_regex_rejection_applies_to_a_streaming_request(self, admin_client, httpx_mock):
        """A regex-only rule reaches streaming requests: the violating reply is
        replaced by the resubmitted one before anything reaches the client."""
        client, _, api_key = admin_client
        await self._setup(client, self.PATTERN)
        self._driver_answers(httpx_mock, self.SLOP, self.CLEAN)

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hello."}],
            "stream": True,
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        streamed = self._streamed_text(resp)
        assert "shivers" not in streamed
        assert streamed.endswith(self.CLEAN)
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert urls == [f"{self.DRIVER_URL}/v1/chat/completions"] * 2

    @pytest.mark.asyncio
    async def test_rule_endpoint_override_runs_without_a_global_judge(self, admin_client, httpx_mock):
        """A rule naming its own endpoint is judged there even with no global
        verification endpoint. Before 0.48.1 such a rule was skipped entirely."""
        from app.services.verification import is_verification_enabled

        judge_url = "http://judge-backend:8888"
        client, _, api_key = admin_client
        rule_id = await self._setup(client, "")
        judge_ep = await client.post("/api/endpoints", json={
            "name": "Judge", "base_url": judge_url, "api_key": "sk-judge",
        })
        updated = await client.put(f"/api/verification/rules/{rule_id}", json={
            "verification_endpoint_id": judge_ep.json()["id"],
        })
        assert updated.status_code == 200, updated.text
        httpx_mock.add_response(
            url=f"{judge_url}/v1/chat/completions",
            json={"id": "verify-1", "choices": [{"message": {
                "role": "assistant",
                "content": '{"violation": false, "reason": "", "severity": "none"}',
            }, "finish_reason": "stop"}]},
            status_code=200,
        )
        self._driver_answers(httpx_mock, self.CLEAN)

        assert await is_verification_enabled(await self._admin_id()) is True
        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Hello."}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == self.CLEAN
        urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert urls.count(f"{judge_url}/v1/chat/completions") == 1
        assert urls.count(f"{self.DRIVER_URL}/v1/chat/completions") == 1


class TestSummarizationDegradationAndEmptyDelta:
    @pytest.mark.asyncio
    async def test_degraded_incremental_preserves_newly_aged_messages(self):
        msgs = [
            {"role": "system", "content": "System card"},
            {"role": "user", "content": "Msg 1 " + "a" * 200},
            {"role": "assistant", "content": "Msg 2 " + "b" * 200},
            {"role": "user", "content": "Msg 3 newly aged " + "c" * 200},
            {"role": "assistant", "content": "Msg 4 newly aged " + "d" * 200},
            {"role": "user", "content": "Msg 5 recent"},
            {"role": "assistant", "content": "Msg 6 recent"},
        ]
        prior_prefix = msgs[1:3]
        existing = SimpleNamespace(
            summary="Summary of msgs 1-2",
            boundary_hash=compute_boundary_hash(prior_prefix),
            message_count=2,
        )
        user_settings = SimpleNamespace(
            summarization_enabled=True,
            summarization_token_threshold=10,
            summarization_keep_recent=2,
            summarization_block_size=0,
            summarization_model="mock-model",
            summarization_prompt="Summarize",
            summarization_parameters_json="[]",
        )
        endpoint = SimpleNamespace(
            id="ep1", name="Mock", base_url="http://mock", api_base_path="",
            api_key="", provider="", parameters_json="[]",
        )

        async def fake_summarize(messages_for_llm, *args, body_json=None, **kwargs):
            if body_json is not None:
                body_json["_gitv_summarizer_degraded"] = True
            return existing.summary

        body_json = {"messages": list(msgs)}
        with (
            patch("app.services.summarization.load_summarization_config", AsyncMock(return_value=(user_settings, endpoint))),
            patch("app.services.summarization.resolve_memory_rule", AsyncMock(return_value=None)),
            patch("app.services.summarization.load_summary", AsyncMock(return_value=existing)),
            patch("app.services.summarization.summarize_messages", side_effect=fake_summarize),
            patch("app.services.summarization.save_summary", AsyncMock()) as mock_save,
        ):
            out = await maybe_summarize(body_json, "u1", "chat1")

        # Never saved a promoted boundary on degradation
        mock_save.assert_not_awaited()
        # Newly aged messages 3 & 4 must NOT be dropped under stale summary!
        contents = [m["content"] for m in out["messages"]]
        assert any("Summary of msgs 1-2" in c for c in contents)
        assert any("Msg 3 newly aged" in c for c in contents)
        assert any("Msg 4 newly aged" in c for c in contents)
        assert any("Msg 5 recent" in c for c in contents)
        # Covered messages 1 & 2 remain compressed under the prior summary
        assert not any("Msg 1 " in c for c in contents)

    @pytest.mark.asyncio
    async def test_empty_delta_does_not_promote_stored_boundary(self):
        msgs = [
            {"role": "system", "content": "System card"},
            {"role": "user", "content": "Msg 1 " + "a" * 200},
            {"role": "assistant", "content": "Msg 2 " + "b" * 200},
            {"role": "user", "content": "   "},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": "Msg 5 recent"},
            {"role": "assistant", "content": "Msg 6 recent"},
        ]
        prior_prefix = msgs[1:3]
        existing = SimpleNamespace(
            summary="Summary of msgs 1-2",
            boundary_hash=compute_boundary_hash(prior_prefix),
            message_count=2,
        )
        user_settings = SimpleNamespace(
            summarization_enabled=True,
            summarization_token_threshold=10,
            summarization_keep_recent=2,
            summarization_block_size=0,
            summarization_model="mock-model",
            summarization_prompt="Summarize",
            summarization_parameters_json="[]",
        )
        endpoint = SimpleNamespace(
            id="ep1", name="Mock", base_url="http://mock", api_base_path="",
            api_key="", provider="", parameters_json="[]",
        )

        body_json = {"messages": list(msgs)}
        with (
            patch("app.services.summarization.load_summarization_config", AsyncMock(return_value=(user_settings, endpoint))),
            patch("app.services.summarization.resolve_memory_rule", AsyncMock(return_value=None)),
            patch("app.services.summarization.load_summary", AsyncMock(return_value=existing)),
            patch("app.services.summarization.save_summary", AsyncMock()) as mock_save,
        ):
            out = await maybe_summarize(body_json, "u1", "chat1")

        # Empty delta must not promote stored boundary_hash / message_count
        mock_save.assert_not_awaited()
        contents = [m["content"] for m in out["messages"]]
        assert any("Summary of msgs 1-2" in c for c in contents)


class TestThinkingLevelValidation:
    @pytest.mark.parametrize("val", ["minimal", "low", "medium", "high", "xhigh", "disable", "none", "HIGH"])
    def test_valid_thinking_levels_accepted(self, val):
        p = ParameterDef(name="thinking_level", type="string", value=val)
        assert p.value == val

    def test_blank_thinking_level_allowed_when_not_required(self):
        p = ParameterDef(name="thinking_level", type="string", value="")
        assert p.value == ""

    def test_typo_thinking_level_rejected_with_validation_error(self):
        with pytest.raises(ValidationError, match="thinking_level"):
            ParameterDef(name="thinking_level", type="string", value="meduim")

    def test_validate_params_rejects_invalid_thinking_level(self):
        with pytest.raises(ValidationError, match="thinking_level"):
            validate_params([{"name": "thinking_level", "type": "string", "value": "turbo"}])
