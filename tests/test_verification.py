import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.models.endpoint import Endpoint
from app.models.verification import VerificationRule
from app.services.verification import (
    VerificationCheckResult,
    VerificationJudgment,
    _apply_resubmission_strategy,
    _build_verification_messages,
    _parse_judgment,
    check_response,
)

# ============================================================================
# Helpers
# ============================================================================

def _mock_endpoint(base_url="https://verify.test", api_key="sk-verify-key"):
    return Endpoint(
        id="ep-1",
        user_id="user-1",
        name="Verify Endpoint",
        base_url=base_url,
        api_key=api_key,
        api_base_path="/api",
        enabled=True,
    )


def _mock_rule(name="Test Rule", prompt="Check for violations", max_retries=2, strategy="add_instructions"):
    return VerificationRule(
        id="rule-1",
        user_id="user-1",
        name=name,
        description="Test verification rule",
        prompt=prompt,
        is_active=True,
        max_retries=max_retries,
        execution_order=10,
        resubmission_strategy=strategy,
    )


def _mock_verification_response(violation=False, reason="", severity="none"):
    content = json.dumps({"violation": violation, "reason": reason, "severity": severity})
    return {
        "id": "verify-1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


def _mock_llm_response(content="Hello there!"):
    return {
        "id": "resp-1",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }


# ============================================================================
# Parse judgment
# ============================================================================

class TestParseJudgment:
    def test_parse_clean_response(self):
        text = '{"violation": false, "reason": "", "severity": "none"}'
        j = _parse_judgment(text, "Test")
        assert j.violation is False
        assert j.severity == "none"
        assert j.passed is True

    def test_parse_violation(self):
        text = '{"violation": true, "reason": "Broke character", "severity": "high"}'
        j = _parse_judgment(text, "Test")
        assert j.violation is True
        assert j.reason == "Broke character"
        assert j.severity == "high"
        assert j.passed is False

    def test_parse_with_surrounding_text(self):
        text = 'Here is my evaluation:\n{"violation": true, "reason": "Bad", "severity": "medium"}\nDone.'
        j = _parse_judgment(text)
        assert j.violation is True
        assert j.reason == "Bad"

    def test_parse_invalid_json(self):
        j = _parse_judgment("This is not JSON at all", "Test")
        assert j.violation is False
        assert "unparseable" in j.reason.lower()

    def test_parse_empty_response(self):
        j = _parse_judgment("", "Test")
        assert j.violation is False


# ============================================================================
# Build verification messages
# ============================================================================

class TestBuildMessages:
    def test_messages_structure(self):
        msgs = _build_verification_messages("Some response", "Some rule")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert "verification" in msgs[0]["content"].lower()
        assert msgs[1]["role"] == "user"
        assert "Some response" in msgs[1]["content"]
        assert "Some rule" in msgs[1]["content"]

    def test_content_truncated(self):
        long_content = "x" * 10000
        msgs = _build_verification_messages(long_content, "rule")
        assert len(msgs[1]["content"]) < 10000


# ============================================================================
# Check response (mocked HTTP)
# ============================================================================

class TestCheckResponse:
    @pytest.mark.asyncio
    async def test_no_violation(self):
        rule = _mock_rule()
        endpoint = _mock_endpoint()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_verification_response(violation=False)

        with patch("app.services.verification.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await check_response("Clean response", [rule], endpoint, "test-model")

        assert result.approved is True
        assert len(result.violations) == 0

    @pytest.mark.asyncio
    async def test_violation_detected(self):
        rule = _mock_rule(prompt="The character must never mention being an AI")
        endpoint = _mock_endpoint()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_verification_response(
            violation=True, reason="Character broke fourth wall", severity="high"
        )

        with patch("app.services.verification.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await check_response(
                "I am an AI language model. How can I help?", [rule], endpoint, "test-model"
            )

        assert result.approved is False
        assert len(result.violations) == 1
        assert result.violations[0].reason == "Character broke fourth wall"
        assert result.violations[0].severity == "high"

    @pytest.mark.asyncio
    async def test_verification_endpoint_error_graceful(self):
        rule = _mock_rule()
        endpoint = _mock_endpoint()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal error"

        with patch("app.services.verification.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await check_response("Some response", [rule], endpoint, "test-model")

        assert result.approved is True
        assert len(result.violations) == 0

    @pytest.mark.asyncio
    async def test_multiple_rules(self):
        rules = [_mock_rule(name="Rule 1"), _mock_rule(name="Rule 2")]
        endpoint = _mock_endpoint()

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = _mock_verification_response(violation=False)

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = _mock_verification_response(
            violation=True, reason="Failed second rule", severity="medium"
        )

        with patch("app.services.verification.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = [resp1, resp2]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            result = await check_response("Test", rules, endpoint, "model")

        assert result.approved is False
        assert len(result.judgments) == 2
        assert len(result.violations) == 1
        assert result.violations[0].rule_name == "Rule 2"


# ============================================================================
# Resubmission strategy
# ============================================================================

class TestResubmissionStrategy:
    def test_add_instructions_strategy(self):
        body = {"model": "test", "messages": [{"role": "user", "content": "Hi"}]}
        violations = [VerificationJudgment(violation=True, reason="Bad output", severity="high")]

        result = _apply_resubmission_strategy(body, violations, "add_instructions")

        assert len(result["messages"]) == 2
        correction = result["messages"][-1]
        assert correction["role"] == "system"
        assert "Bad output" in correction["content"]
        assert "[Verification Correction]" in correction["content"]

    def test_rewrite_strategy(self):
        body = {
            "model": "test",
            "messages": [
                {"role": "user", "content": "Write a story"},
                {"role": "assistant", "content": "Bad response that needs rewriting"},
            ],
        }
        violations = [VerificationJudgment(violation=True, reason="Poor quality", severity="medium")]

        result = _apply_resubmission_strategy(body, violations, "rewrite")

        messages = result["messages"]
        assert any("Bad response that needs rewriting" in m.get("content", "") for m in messages)
        assert messages[-1]["role"] == "user"
        assert "rewrite" in messages[-1]["content"].lower()

    def test_original_body_not_mutated(self):
        body = {"model": "test", "messages": [{"role": "user", "content": "Hi"}]}
        original_len = len(body["messages"])
        violations = [VerificationJudgment(violation=True, reason="Bad", severity="low")]

        _apply_resubmission_strategy(body, violations, "add_instructions")

        assert len(body["messages"]) == original_len


# ============================================================================
# API: Verification Rule CRUD
# ============================================================================

class TestRuleCRUD:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_create_rule(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/verification/rules", json={
            "name": "Character Check",
            "prompt": "The response must stay in character at all times.",
            "max_retries": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Character Check"
        assert data["max_retries"] == 3
        assert data["is_active"] is True

    @pytest.mark.asyncio
    async def test_list_rules(self, admin_client):
        client, _, _ = admin_client
        await client.post("/api/verification/rules", json={"name": "Rule 1", "prompt": "Check 1"})
        await client.post("/api/verification/rules", json={"name": "Rule 2", "prompt": "Check 2"})
        resp = await client.get("/api/verification/rules")
        assert resp.status_code == 200
        assert len(resp.json()["rules"]) == 2

    @pytest.mark.asyncio
    async def test_get_rule(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/verification/rules", json={
            "name": "Detail Rule", "prompt": "Detailed prompt"
        })
        rule_id = create.json()["id"]
        resp = await client.get(f"/api/verification/rules/{rule_id}")
        assert resp.status_code == 200
        assert resp.json()["prompt"] == "Detailed prompt"

    @pytest.mark.asyncio
    async def test_update_rule(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/verification/rules", json={
            "name": "Original", "prompt": "Original prompt"
        })
        rule_id = create.json()["id"]
        resp = await client.put(f"/api/verification/rules/{rule_id}", json={
            "name": "Updated",
            "max_retries": 5,
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["max_retries"] == 5
        assert resp.json()["is_active"] is False

    @pytest.mark.asyncio
    async def test_delete_rule(self, admin_client):
        client, _, _ = admin_client
        create = await client.post("/api/verification/rules", json={
            "name": "Delete Me", "prompt": "test"
        })
        rule_id = create.json()["id"]
        resp = await client.delete(f"/api/verification/rules/{rule_id}")
        assert resp.status_code == 204
        resp = await client.get(f"/api/verification/rules/{rule_id}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rule_not_found(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/verification/rules/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_create_exceeds_prompt_size_limit(self, admin_client):
        from app.services.admin import update_admin_settings
        client, _, _ = admin_client
        try:
            await update_admin_settings({"max_rule_size_kb": 1})
            resp = await client.post("/api/verification/rules", json={
                "name": "TooBig", "prompt": "x" * 2000,
            })
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_rule_size_kb": 25})

    @pytest.mark.asyncio
    async def test_create_strips_control_chars_in_prompt(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/verification/rules", json={
            "name": "Clean", "prompt": "check\x00this",
        })
        assert resp.status_code == 201
        assert resp.json()["prompt"] == "checkthis"


# ============================================================================
# API: Verification Settings
# ============================================================================

class TestVerificationSettings:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_get_default_settings(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/verification/settings")
        assert resp.status_code == 200
        assert resp.json()["verification_enabled"] is False

    @pytest.mark.asyncio
    async def test_update_settings(self, admin_client):
        client, _, _ = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Verify Endpoint",
            "base_url": "https://verify.test",
            "api_key": "sk-verify",
        })
        endpoint_id = ep_resp.json()["id"]

        resp = await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": endpoint_id,
            "verification_model": "Gemma-4-31B-it",
        })
        assert resp.status_code == 200
        assert resp.json()["verification_enabled"] is True
        assert resp.json()["verification_model"] == "Gemma-4-31B-it"


# ============================================================================
# API: Verification Test Endpoint
# ============================================================================

class TestVerificationTestAPI:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_test_endpoint_no_endpoint_configured(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post("/api/verification/test", json={
            "content": "Some response",
            "prompt": "Check for issues",
        })
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_test_endpoint_with_endpoint(self, admin_client):
        client, _, _ = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Verify LLM",
            "base_url": "https://verify.test",
            "api_key": "sk-verify",
            "api_base_path": "/api",
        })
        endpoint_id = ep_resp.json()["id"]

        mock_result = VerificationCheckResult(
            approved=False,
            judgments=[VerificationJudgment(
                violation=True, reason="Character broke", severity="high", rule_name="ad-hoc"
            )],
            violations=[VerificationJudgment(
                violation=True, reason="Character broke", severity="high", rule_name="ad-hoc"
            )],
        )

        with patch("app.routers.verification.check_response", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post("/api/verification/test", json={
                "content": "I am an AI model",
                "prompt": "Check if character stays in character",
                "endpoint_id": endpoint_id,
                "model": "Gemma-4-31B-it",
            })

        assert resp.status_code == 200
        data = resp.json()
        assert data["violation"] is True
        assert data["approved"] is False


# ============================================================================
# API: Verification Logs
# ============================================================================

class TestVerificationLogs:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    @pytest.mark.asyncio
    async def test_empty_logs(self, admin_client):
        client, _, _ = admin_client
        resp = await client.get("/api/verification/logs")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["logs"] == []


# ============================================================================
# Proxy Integration: Verification modifies response
# ============================================================================

class TestProxyVerificationIntegration:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    MOCK_ENDPOINT_URL = "http://mock-backend:9999"

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        test_settings = Settings(
            default_endpoint_url=self.MOCK_ENDPOINT_URL,
            default_endpoint_api_key="sk-test-key",
            default_endpoint_model="test-model",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

    @pytest.mark.asyncio
    async def test_verification_passes_through_when_disabled(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Test", "base_url": self.MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        await client.put("/api/settings", json={"default_endpoint_id": ep_resp.json()["id"]})

        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_llm_response("Hello!"),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Hello!"

    @pytest.mark.asyncio
    async def test_verification_retry_succeeds(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Main", "base_url": self.MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        endpoint_id = ep_resp.json()["id"]
        await client.put("/api/settings", json={"default_endpoint_id": endpoint_id})

        await client.post("api/verification/rules", json={
            "name": "Char Check",
            "prompt": "Must stay in character",
            "max_retries": 2,
        })

        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": endpoint_id,
            "verification_model": "test-model",
        })

        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_llm_response("I am an AI assistant."),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_llm_response("Greetings, traveler! I am Aria."),
            status_code=200,
        )

        violation_result = VerificationCheckResult(
            approved=False,
            judgments=[VerificationJudgment(violation=True, reason="Broke character", severity="high")],
            violations=[VerificationJudgment(violation=True, reason="Broke character", severity="high")],
        )
        pass_result = VerificationCheckResult(
            approved=True,
            judgments=[VerificationJudgment(violation=False, reason="", severity="none")],
            violations=[],
        )

        with patch(
            "app.services.verification.check_response",
            new_callable=AsyncMock,
            side_effect=[violation_result, pass_result],
        ):
            resp = await client.post("/v1/chat/completions", json={
                "model": "test",
                "messages": [{"role": "user", "content": "Hi"}],
            }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "Aria" in content

    @pytest.mark.asyncio
    async def test_verification_max_retries_returns_last(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        ep_resp = await client.post("/api/endpoints", json={
            "name": "Main", "base_url": self.MOCK_ENDPOINT_URL, "api_key": "sk-test",
        })
        endpoint_id = ep_resp.json()["id"]
        await client.put("/api/settings", json={"default_endpoint_id": endpoint_id})

        await client.post("api/verification/rules", json={
            "name": "Strict Check",
            "prompt": "Must be perfect",
            "max_retries": 1,
        })

        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": endpoint_id,
            "verification_model": "test-model",
        })

        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_llm_response("Bad response 1"),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{self.MOCK_ENDPOINT_URL}/v1/chat/completions",
            json=_mock_llm_response("Bad response 2"),
            status_code=200,
        )

        always_violation = VerificationCheckResult(
            approved=False,
            judgments=[VerificationJudgment(violation=True, reason="Still bad", severity="high")],
            violations=[VerificationJudgment(violation=True, reason="Still bad", severity="high")],
        )

        with patch("app.services.verification.check_response", new_callable=AsyncMock, return_value=always_violation):
            resp = await client.post("/v1/chat/completions", json={
                "model": "test",
                "messages": [{"role": "user", "content": "Hi"}],
            }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        assert "Bad response 2" in content

    @pytest.mark.asyncio
    async def test_verification_loop_runs_real_path(self, admin_client, httpx_mock):
        """Exercise the real parse -> strategy -> retry path by mocking ONLY the
        upstream HTTP layer (not check_response). A violation verdict from the
        verifier must trigger one driver retry, after which a clean verdict is
        approved and returned. If _parse_judgment, _build_verification_messages,
        or _apply_resubmission_strategy regresses, this fails."""
        client, _, api_key = admin_client

        # Distinct URLs so httpx_mock routes driver and verifier calls unambiguously.
        driver_url = "http://driver-backend:9999"
        verify_url = "http://verify-backend:8888"

        driver_ep = await client.post("/api/endpoints", json={
            "name": "Main", "base_url": driver_url, "api_key": "sk-test",
        })
        await client.put("/api/settings", json={"default_endpoint_id": driver_ep.json()["id"]})

        verify_ep = await client.post("/api/endpoints", json={
            "name": "Verifier", "base_url": verify_url, "api_key": "sk-verify", "api_base_path": "/v1",
        })

        await client.post("/api/verification/rules", json={
            "name": "Char Check",
            "prompt": "Response must stay in character.",
            "max_retries": 2,
        })
        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": verify_ep.json()["id"],
            "verification_model": "test-model",
        })

        # Driver: violating response first, clean response on retry.
        httpx_mock.add_response(
            url=f"{driver_url}/v1/chat/completions",
            json=_mock_llm_response("I am an AI language model."),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{driver_url}/v1/chat/completions",
            json=_mock_llm_response("Greetings! I am Aria the traveler."),
            status_code=200,
        )
        # Verifier: flag first as violation, then approve.
        httpx_mock.add_response(
            url=f"{verify_url}/v1/chat/completions",
            json=_mock_verification_response(violation=True, reason="Broke character", severity="high"),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{verify_url}/v1/chat/completions",
            json=_mock_verification_response(violation=False),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        # The clean (retried) response is returned, proving parse+strategy+retry ran.
        assert "Aria" in content


# ============================================================================
# Tag activation: a rule with is_active=False + tag fires only when its
# <#verify-tag#> activation tag is present in the prompt.
# ============================================================================


class TestVerificationTagActivation:
    @pytest.fixture
    async def client(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac

    @pytest.fixture
    async def admin_client(self, client):
        setup_resp = await client.post(
            "/api/auth/setup",
            json={"username": "admin", "password": "adminpass123"},
        )
        assert setup_resp.status_code == 201
        token = setup_resp.json()["access_token"]
        api_key = setup_resp.json()["api_key"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, token, api_key

    DRIVER_URL = "http://mock-backend:9999"
    VERIFY_URL = "http://verify-backend:8888"

    @pytest.fixture(autouse=True)
    def set_endpoint(self, monkeypatch):
        from app.config import Settings
        test_settings = Settings(
            default_endpoint_url=self.DRIVER_URL,
            default_endpoint_api_key="sk-test-key",
            default_endpoint_model="test-model",
        )
        monkeypatch.setattr("app.services.proxy.settings", test_settings)

    async def _setup_tagged_rule(self, client):
        """Create a driver endpoint (default) + a distinct verification endpoint,
        enable verification, and create one rule that is inactive but tag-gated."""
        driver_ep = await client.post("/api/endpoints", json={
            "name": "Driver", "base_url": self.DRIVER_URL, "api_key": "sk-driver",
        })
        await client.put("/api/settings", json={"default_endpoint_id": driver_ep.json()["id"]})

        verify_ep = await client.post("/api/endpoints", json={
            "name": "Verifier", "base_url": self.VERIFY_URL, "api_key": "sk-verify",
            "api_base_path": "/v1",
        })

        await client.post("/api/verification/rules", json={
            "name": "Char Mode Check",
            "prompt": "Response must stay in character.",
            "max_retries": 2,
            "is_active": False,
            "tag": "charmode",
        })

        await client.put("/api/verification/settings", json={
            "verification_enabled": True,
            "verification_endpoint_id": verify_ep.json()["id"],
            "verification_model": "verify-model",
        })

    @pytest.mark.asyncio
    async def test_tag_absent_rule_does_not_fire(self, admin_client, httpx_mock):
        """Inactive rule with a tag must NOT run verification when the tag is absent."""
        client, _, api_key = admin_client
        await self._setup_tagged_rule(client)

        httpx_mock.add_response(
            url=f"{self.DRIVER_URL}/v1/chat/completions",
            json=_mock_llm_response("Plain driver response, no verification."),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hi, no activation tag here."}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "Plain driver response, no verification."
        # Verification endpoint must NOT have been hit: the only recorded request
        # is the single driver call.
        assert len(httpx_mock.get_requests()) == 1
        assert self.VERIFY_URL not in str(httpx_mock.get_requests()[-1].url)

    @pytest.mark.asyncio
    async def test_tag_present_rule_fires(self, admin_client, httpx_mock):
        """Same inactive+tagged rule, but the prompt carries <#verify-charmode#>:
        the rule activates, verification runs, and a violation triggers a retry."""
        client, _, api_key = admin_client
        await self._setup_tagged_rule(client)

        # Driver: first a violating response, then a clean one on retry.
        httpx_mock.add_response(
            url=f"{self.DRIVER_URL}/v1/chat/completions",
            json=_mock_llm_response("I am an AI assistant, not a character."),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{self.DRIVER_URL}/v1/chat/completions",
            json=_mock_llm_response("Greetings, traveler! I am Aria the smith."),
            status_code=200,
        )
        # Verifier: flag the first response as a violation, then approve.
        httpx_mock.add_response(
            url=f"{self.VERIFY_URL}/v1/chat/completions",
            json=_mock_verification_response(violation=True, reason="Broke character", severity="high"),
            status_code=200,
        )
        httpx_mock.add_response(
            url=f"{self.VERIFY_URL}/v1/chat/completions",
            json=_mock_verification_response(violation=False),
            status_code=200,
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test",
            "messages": [{"role": "user", "content": "Hello. <#verify-charmode#>"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        content = resp.json()["choices"][0]["message"]["content"]
        # The retried (clean) response is what the user sees.
        assert "Aria" in content
        # Both endpoints were exercised: driver (twice) + verifier (twice).
        all_urls = [str(r.url) for r in httpx_mock.get_requests()]
        assert any(self.VERIFY_URL in u for u in all_urls), "Verification endpoint was never hit"
        assert any(self.DRIVER_URL in u for u in all_urls), "Driver endpoint was never hit"
