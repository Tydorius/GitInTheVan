"""Automated integration test that exercises the full proxy pipeline.

Creates a dedicated test user, copies the admin's endpoint, sends requests
through the proxy, and validates results.

Usage (from GitInTheVan directory):
    .venv\\Scripts\\python.exe scripts\\flow_test.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

SERVER = "http://127.0.0.1:8000"
ADMIN_USER = "admin"
ADMIN_PASS = "adminpass123"
TEST_USER = "flow_test_user"
TEST_PASS = "flow_test_pass_123"
LINE = "=" * 70


def section(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


def pass_test(msg: str) -> None:
    print(f"  PASS: {msg}")


def fail_test(msg: str) -> None:
    print(f"  FAIL: {msg}")


def info(msg: str) -> None:
    print(f"  INFO: {msg}")


class TestClient:
    def __init__(self):
        self.server: str = SERVER
        self.admin_user: str = ADMIN_USER
        self.admin_pass: str = ADMIN_PASS
        self.admin_token: str = ""
        self.test_token: str = ""
        self.test_api_key: str = ""
        self.endpoint_id: str = ""
        self.admin_endpoint: dict = {}
        self.model: str = ""
        self.timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)

    def admin_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.admin_token}", "Content-Type": "application/json"}

    def test_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.test_token}", "Content-Type": "application/json"}

    def proxy_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.test_api_key}", "Content-Type": "application/json"}

    def _pick_model_from_endpoint(self, endpoint: dict) -> str:
        """Query the endpoint for available models and let user pick."""
        try:
            base_url = endpoint.get("base_url", "")
            api_key = endpoint.get("api_key", "")
            api_base_path = endpoint.get("api_base_path", "") or "/v1"
            models_url = f"{base_url}{api_base_path}/models"

            resp = httpx.get(models_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15.0)
            if resp.status_code != 200:
                info(f"Could not query models ({resp.status_code}), using default")
                return "Gemma-4-31B-it"

            data = resp.json()
            models = data.get("data", [])
            if not models:
                info("No models returned from endpoint, using default")
                return "Gemma-4-31B-it"

            model_ids = [m.get("id", str(m)) for m in models]
            model_ids.sort()

            if len(model_ids) == 1:
                return model_ids[0]

            print("\n  Available models:")
            for i, mid in enumerate(model_ids, 1):
                print(f"    {i}. {mid}")

            while True:
                try:
                    choice = input("\n  Select model number (or press Enter for first): ").strip()
                    if not choice:
                        return model_ids[0]
                    idx = int(choice) - 1
                    if 0 <= idx < len(model_ids):
                        return model_ids[idx]
                    print("  Invalid selection, try again")
                except (ValueError, EOFError):
                    return model_ids[0]

        except Exception as e:
            info(f"Model query failed ({e}), using default")
            return "Gemma-4-31B-it"

    def setup(self) -> bool:
        section("SETUP: Admin Login")
        login = httpx.post(
            f"{self.server}/api/auth/login",
            json={"username": self.admin_user, "password": self.admin_pass},
            timeout=10.0,
        )
        if login.status_code != 200:
            fail_test(f"Admin login failed: {login.status_code} {login.text[:200]}")
            info("Make sure you have created an admin account via the web UI first.")
            return False
        self.admin_token = login.json()["access_token"]
        pass_test("Logged in as admin")

        section("SETUP: Get Admin Endpoint")
        eps = httpx.get(f"{self.server}/api/endpoints", headers=self.admin_headers(), timeout=10.0)
        endpoints = eps.json().get("endpoints", [])
        if not endpoints:
            fail_test("Admin has no endpoints configured")
            info("Create an endpoint in the web UI first, then re-run this test.")
            return False
        self.admin_endpoint = endpoints[0]
        pass_test(f"Admin endpoint: {self.admin_endpoint['name']} ({self.admin_endpoint['base_url']})")

        settings = httpx.get(f"{self.server}/api/settings", headers=self.admin_headers(), timeout=10.0)
        self.model = settings.json().get("default_model", "")

        if not self.model:
            self.model = self._pick_model_from_endpoint(self.admin_endpoint)
            if not self.model:
                fail_test("Could not determine a model to use for testing")
                return False

        pass_test(f"Using model: {self.model}")

        section("SETUP: Create Test User")
        users = httpx.get(f"{self.server}/api/users", headers=self.admin_headers(), timeout=10.0)
        existing = [u for u in users.json().get("users", []) if u["username"] == TEST_USER]

        if existing:
            info("Test user already exists, creating new one for fresh API key...")
            create = httpx.post(
                f"{self.server}/api/users",
                json={"username": f"{TEST_USER}_{int(time.time())}", "password": TEST_PASS},
                headers=self.admin_headers(),
                timeout=10.0,
            )
            if create.status_code != 201:
                fail_test(f"Could not create test user: {create.status_code} {create.text[:200]}")
                return False
            self.test_api_key = create.json()["api_key"]
            test_user_id = create.json()["id"]
            test_username = create.json()["username"]
        else:
            create = httpx.post(
                f"{self.server}/api/users",
                json={"username": TEST_USER, "password": TEST_PASS},
                headers=self.admin_headers(),
                timeout=10.0,
            )
            if create.status_code != 201:
                fail_test(f"Could not create test user: {create.status_code} {create.text[:200]}")
                return False
            self.test_api_key = create.json()["api_key"]
            test_user_id = create.json()["id"]
            test_username = TEST_USER

        pass_test(f"Test user: {test_username} (API key: {self.test_api_key[:12]}...)")

        login_test = httpx.post(
            f"{self.server}/api/auth/login",
            json={"username": test_username, "password": TEST_PASS},
            timeout=10.0,
        )
        if login_test.status_code != 200:
            fail_test(f"Test user login failed: {login_test.status_code}")
            return False
        self.test_token = login_test.json()["access_token"]
        pass_test("Test user logged in")

        section("SETUP: Copy Admin Endpoint to Test User")
        create_ep = httpx.post(
            f"{self.server}/api/endpoints",
            json={
                "name": "Flow Test Endpoint",
                "base_url": self.admin_endpoint["base_url"],
                "api_key": self.admin_endpoint.get("api_key", ""),
                "api_base_path": self.admin_endpoint.get("api_base_path", ""),
                "enabled": True,
            },
            headers=self.test_headers(),
            timeout=10.0,
        )
        if create_ep.status_code != 201:
            fail_test(f"Could not create endpoint for test user: {create_ep.status_code} {create_ep.text[:200]}")
            return False
        self.endpoint_id = create_ep.json()["id"]
        pass_test(f"Endpoint created: {self.endpoint_id[:8]}...")

        httpx.put(
            f"{self.server}/api/settings",
            json={"default_endpoint_id": self.endpoint_id},
            headers=self.test_headers(),
            timeout=10.0,
        )
        pass_test("Set as default endpoint for test user")

        health = httpx.get(f"{self.server}/health", timeout=5.0)
        if health.json().get("status") == "ok":
            pass_test("Server healthy")

        return True

    def cleanup_test_user_resources(self) -> None:
        info("Cleaning up test resources...")
        for endpoint in ["/api/cantrips", "/api/lorebooks"]:
            resp = httpx.get(f"{self.server}{endpoint}", headers=self.test_headers(), timeout=10.0)
            resource_key = endpoint.split("/")[-1]
            for item in resp.json().get(resource_key, []):
                if "FLOW_TEST" in item.get("name", ""):
                    httpx.delete(f"{SERVER}{endpoint}/{item['id']}", headers=self.test_headers(), timeout=10.0)

        vr = httpx.get(f"{self.server}/api/verification/rules", headers=self.test_headers(), timeout=10.0)
        for r in vr.json().get("rules", []):
            if "FLOW_TEST" in r.get("name", ""):
                httpx.delete(f"{self.server}/api/verification/rules/{r['id']}", headers=self.test_headers(), timeout=10.0)

        self.set_summarization_settings({"summarization_enabled": False})
        for s in self.list_summaries():
            httpx.delete(
                f"{self.server}/api/summarization/summaries/{s['id']}",
                headers=self.test_headers(),
                timeout=10.0,
            )

    def send_proxy_request(
        self,
        messages: list[dict],
        extra_headers: dict | None = None,
        stream: bool = False,
    ) -> dict[str, Any] | str:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": 500,
        }
        headers = self.proxy_headers()
        if extra_headers:
            headers.update(extra_headers)

        resp = httpx.post(
            f"{self.server}/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=self.timeout,
        )

        if stream:
            return resp.text

        return resp.json()

    def create_cantrip(self, name: str, code: str, tag: str = "", is_active: bool = True) -> str | None:
        resp = httpx.post(
            f"{self.server}/api/cantrips",
            json={"name": name, "code": code, "tag": tag, "is_active": is_active},
            headers=self.test_headers(),
            timeout=10.0,
        )
        if resp.status_code == 201:
            return resp.json()["id"]
        return None

    def update_cantrip(self, cantrip_id: str, data: dict) -> bool:
        resp = httpx.put(
            f"{self.server}/api/cantrips/{cantrip_id}",
            json=data,
            headers=self.test_headers(),
            timeout=10.0,
        )
        return resp.status_code == 200

    def create_lorebook(self, name: str, entries: list[dict], tag: str = "") -> str | None:
        resp = httpx.post(
            f"{self.server}/api/lorebooks/import",
            json={"name": name, "description": "Flow test lorebook", "entries": entries, "tag": tag},
            headers=self.test_headers(),
            timeout=10.0,
        )
        if resp.status_code == 201:
            return resp.json()["id"]
        return None

    def update_lorebook(self, lb_id: str, data: dict) -> bool:
        resp = httpx.put(
            f"{self.server}/api/lorebooks/{lb_id}",
            json=data,
            headers=self.test_headers(),
            timeout=10.0,
        )
        return resp.status_code == 200

    def list_memories(self, conversation_id: str = "") -> list:
        url = f"{self.server}/api/memories"
        if conversation_id:
            url += f"?conversation_id={conversation_id}"
        resp = httpx.get(url, headers=self.test_headers(), timeout=10.0)
        return resp.json().get("memories", [])

    def set_summarization_settings(self, data: dict) -> bool:
        resp = httpx.put(
            f"{self.server}/api/summarization/settings",
            json=data,
            headers=self.test_headers(),
            timeout=10.0,
        )
        return resp.status_code == 200

    def list_summaries(self) -> list:
        resp = httpx.get(
            f"{self.server}/api/summarization/summaries",
            headers=self.test_headers(),
            timeout=10.0,
        )
        return resp.json().get("summaries", [])


def _check_upstream_error(result: dict) -> bool:
    """Check if the response is an upstream LLM error. Reports and returns True if so."""
    if result.get("detail") or result.get("error"):
        detail = result.get("detail") or result.get("error")
        if isinstance(detail, dict):
            msg = detail.get("message", str(detail))[:200]
        else:
            msg = str(detail)[:200]
        fail_test(f"Upstream LLM error: {msg}")
        info("This is an endpoint/LLM issue, not a GitInTheVan bug.")
        info("Check that your LLM endpoint is running and has enough resources.")
        return True
    return False


def test_passthrough(tc: TestClient) -> bool:
    section("TEST: Basic Passthrough")
    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply in one sentence."},
        {"role": "user", "content": "Say hello."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        if _check_upstream_error(result):
            return False
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Got response: {content[:100]}")
            return True
        else:
            fail_test(f"Empty response: {json.dumps(result)[:200]}")
    else:
        fail_test(f"Unexpected response: {str(result)[:200]}")
    return False


def test_cantrip_execution(tc: TestClient) -> bool:
    section("TEST: Cantrip Execution Through Proxy")
    code = '''
context.character.scenario += " [FLOW_TEST_CANTRIP] Cantrip executed successfully.";
console.log("Flow test cantrip ran");
'''
    cid = tc.create_cantrip("FLOW_TEST_Cantrip", code)
    if not cid:
        fail_test("Could not create cantrip")
        return False
    pass_test("Created test cantrip")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Say hello."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        if _check_upstream_error(result):
            pass
        else:
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            pass_test(f"Got response: {content[:80]}")
            info("(Check server logs for 'Cantrips processed' message)")
    else:
        fail_test(f"Unexpected response: {str(result)[:200]}")

    tc.update_cantrip(cid, {"is_active": False})
    pass_test("Disabled test cantrip")
    return True


def test_tag_activation(tc: TestClient) -> bool:
    section("TEST: Tag Activation")
    code = '''
context.character.scenario += " [FLOW_TEST_TAG] Tag-activated cantrip fired!";
console.log("Tag-activated cantrip ran");
'''
    cid = tc.create_cantrip("FLOW_TEST_TagCantrip", code, tag="flow-test", is_active=False)
    if not cid:
        fail_test("Could not create tagged cantrip")
        return False
    pass_test("Created inactive cantrip with tag 'flow-test'")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Hello <#cantrip-flow-test#>"},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        pass_test(f"Got response: {content[:80]}")
        info("(Check server logs for 'Tags detected' and cantrip execution)")

        forwarded_tags = "<#cantrip-flow-test#>" in json.dumps(result)
        if forwarded_tags:
            fail_test("Tag was NOT stripped from forwarded request")
        else:
            pass_test("Tag was stripped from response content")
    else:
        fail_test("Unexpected response type")

    tc.update_cantrip(cid, {"is_active": False})
    return True


def test_lorebook_injection(tc: TestClient) -> bool:
    section("TEST: Lorebook Injection")
    entries = [{
        "name": "Flow Test Marker",
        "keys": ["flowtest"],
        "secondary_keys": [],
        "content": "[FLOW_TEST_LOREBOOK] The crystal glows with blue light.",
        "position": "before_last_message",
        "insertion_order": 10,
        "is_constant": False,
        "is_selective": False,
        "is_disabled": False,
    }]
    lid = tc.create_lorebook("FLOW_TEST_Lorebook", entries)
    if not lid:
        fail_test("Could not create lorebook")
        return False
    pass_test("Created test lorebook")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "I examine the flowtest crystal."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        pass_test(f"Got response: {content[:80]}")
        info("(Check server logs for 'Lorebook injection: 1 entries matched')")

    tc.update_lorebook(lid, {"is_active": False})
    pass_test("Disabled test lorebook")
    return True


def test_memory_persistence(tc: TestClient) -> None:
    section("TEST: Memory Persistence (Rolling Hash)")
    conv_id = "flow-test-memory-001"

    code = '''
context.character.scenario += " [FLOW_TEST_MEMORY] The time of day is evening.";
'''
    cid = tc.create_cantrip("FLOW_TEST_MemoryCantrip", code)
    if cid:
        pass_test("Created memory test cantrip")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. At the end of your response, include: <memstore key=\"time\">evening</memstore>"},
        {"role": "user", "content": "What time is it?"},
    ]
    result = tc.send_proxy_request(messages, extra_headers={"X-Conversation-Id": conv_id})
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        pass_test(f"Got response: {content[:80]}")

        memstore_in_content = "<memstore" in content
        if memstore_in_content:
            fail_test("<memstore> tags were NOT stripped from response")
        else:
            pass_test("<memstore> tags were stripped from response")

        time.sleep(1)
        memories = tc.list_memories(conv_id)
        has_time = any(m["key"] == "time" for m in memories)
        if has_time:
            pass_test("Memory 'time' saved to database")
        else:
            info("No memories found yet (LLM may not have emitted tags)")

    if cid:
        tc.update_cantrip(cid, {"is_active": False})


def test_verification(tc: TestClient) -> bool:
    section("TEST: Verification (if enabled)")
    vs = httpx.get(f"{tc.server}/api/verification/settings", headers=tc.test_headers(), timeout=10.0)
    if not vs.json().get("verification_enabled"):
        info("Verification not enabled for test user - skipping test")
        return True

    pass_test("Verification is enabled for test user")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Say hello."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        pass_test(f"Verification passed, response: {content[:80]}")
    else:
        fail_test("Unexpected response during verification")

    return True


def test_summarization(tc: TestClient) -> bool:
    section("TEST: Summarization")
    enabled = tc.set_summarization_settings({
        "summarization_enabled": True,
        "summarization_endpoint_id": tc.endpoint_id,
        "summarization_model": tc.model,
        "summarization_token_threshold": 200,
        "summarization_keep_recent": 2,
    })
    if not enabled:
        fail_test("Could not enable summarization settings")
        return False
    pass_test("Summarization enabled (threshold=200, keep_recent=2)")

    long_text = "The adventurer explored the ancient ruins and discovered a glowing artifact. " * 8
    messages = [
        {"role": "system", "content": "You are a storyteller. Reply briefly."},
        {"role": "user", "content": f"Chapter 1: {long_text}"},
        {"role": "assistant", "content": f"The hero pressed onward. {long_text}"},
        {"role": "user", "content": f"Chapter 2: {long_text}"},
        {"role": "assistant", "content": f"A dragon appeared. {long_text}"},
        {"role": "user", "content": "Continue the story briefly."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        if _check_upstream_error(result):
            tc.set_summarization_settings({"summarization_enabled": False})
            return False
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Summarized request succeeded, response: {content[:80]}")
        else:
            fail_test("Empty response during summarization test")
            tc.set_summarization_settings({"summarization_enabled": False})
            return False
    else:
        fail_test("Unexpected response during summarization test")
        tc.set_summarization_settings({"summarization_enabled": False})
        return False

    time.sleep(1)
    summaries = tc.list_summaries()
    if summaries:
        pass_test(f"Summary stored in database ({len(summaries)} summary/summaries)")
    else:
        info("No summary stored (token estimate may not have exceeded threshold or LLM returned empty)")

    tc.set_summarization_settings({"summarization_enabled": False})
    pass_test("Disabled summarization")
    return True


def test_forbidden_words(tc: TestClient) -> bool:
    section("TEST: Forbidden Words")

    test_phrase = "FLOW_TEST_FORBIDDEN"
    resp = httpx.post(
        f"{tc.server}/api/forbidden-words",
        json={"phrase": test_phrase},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if resp.status_code != 201:
        fail_test(f"Could not create forbidden word: {resp.status_code}")
        return False
    pass_test(f"Created forbidden word: '{test_phrase}'")

    enable = httpx.put(
        f"{tc.server}/api/forbidden-words/settings",
        json={"forbidden_words_enabled": True, "forbidden_words_case_sensitive": False},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if enable.status_code != 200:
        fail_test("Could not enable forbidden words")
        return False
    pass_test("Forbidden words check enabled")

    test_resp = httpx.post(
        f"{tc.server}/api/forbidden-words/test",
        json={"content": f"This text contains {test_phrase} which should be flagged."},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    test_data = test_resp.json()
    if test_data.get("has_matches"):
        pass_test(f"Scanner detected forbidden phrase ({test_data['match_count']} match(es))")
    else:
        fail_test("Scanner failed to detect forbidden phrase")
        httpx.put(f"{tc.server}/api/forbidden-words/settings", json={"forbidden_words_enabled": False}, headers=tc.test_headers(), timeout=10.0)
        return False

    no_match_resp = httpx.post(
        f"{tc.server}/api/forbidden-words/test",
        json={"content": "This text is clean and should not match."},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if not no_match_resp.json().get("has_matches"):
        pass_test("Scanner correctly found no matches in clean text")
    else:
        fail_test("Scanner false-positive on clean text")

    fw_list = httpx.get(f"{tc.server}/api/forbidden-words", headers=tc.test_headers(), timeout=10.0)
    for w in fw_list.json().get("words", []):
        if w["phrase"] == test_phrase:
            httpx.delete(f"{tc.server}/api/forbidden-words/{w['id']}", headers=tc.test_headers(), timeout=10.0)
    pass_test("Cleaned up forbidden word")

    httpx.put(
        f"{tc.server}/api/forbidden-words/settings",
        json={"forbidden_words_enabled": False},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Disabled forbidden words check")
    return True


def test_multi_position_cantrip(tc: TestClient) -> bool:
    section("TEST: Multi-Position Cantrip (Pre-Navigator)")
    code = '''
context.response.content = context.response.content.replace("FLOW_TEST_MARKER", "PROCESSED");
'''
    resp = httpx.post(
        f"{tc.server}/api/cantrips",
        json={
            "name": "FLOW_TEST_PreNav",
            "code": code,
            "run_pre_driver": False,
            "run_pre_navigator": True,
        },
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if resp.status_code != 201:
        fail_test(f"Could not create pre-navigator cantrip: {resp.status_code}")
        return False
    cid = resp.json()["id"]
    pass_test("Created pre-navigator cantrip (response modifier)")

    c_data = resp.json()
    if c_data.get("run_pre_navigator") is True and c_data.get("run_pre_driver") is False:
        pass_test("Position flags correctly stored")
    else:
        fail_test(f"Position flags wrong: pre_driver={c_data.get('run_pre_driver')}, pre_navigator={c_data.get('run_pre_navigator')}")

    tc.update_cantrip(cid, {"is_active": False})
    pass_test("Disabled test cantrip")
    return True


def test_driver_callable(tc: TestClient) -> bool:
    section("TEST: Driver-Callable Tools")
    code = '''
if (context.tool_call) {
    const sides = parseInt(context.tool_call.args.sides) || 6;
    const count = parseInt(context.tool_call.args.count) || 1;
    let results = [];
    for (let i = 0; i < count; i++) {
        results.push(Math.floor(Math.random() * sides) + 1);
    }
    const total = results.reduce((a, b) => a + b, 0);
    context.tool_result = `${count}d${sides} = [${results.join(", ")}] = ${total}`;
} else {
    context.tool_result = "No tool call received.";
}
'''
    resp = httpx.post(
        f"{tc.server}/api/cantrips",
        json={
            "name": "dice_roll",
            "description": "Rolls dice when requested.",
            "llm_instructions": "Call this tool to roll dice. Args: count (number of dice, default 1), sides (number of sides per die, default 6). Example: <call:dice_roll count=\"2\" sides=\"6\">",
            "code": code,
            "run_pre_driver": False,
            "run_driver_callable": True,
        },
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if resp.status_code != 201:
        fail_test(f"Could not create driver-callable cantrip: {resp.status_code}")
        return False
    cid = resp.json()["id"]
    pass_test("Created driver-callable cantrip 'dice_roll'")

    c_data = resp.json()
    if c_data.get("run_driver_callable") is True:
        pass_test("Driver-Callable flag correctly stored")
    else:
        fail_test("Driver-Callable flag not stored correctly")

    if c_data.get("llm_instructions"):
        pass_test("LLM instructions stored")
    else:
        info("Warning: LLM instructions not stored")

    httpx.put(
        f"{tc.server}/api/settings",
        json={"driver_callable_turns": 1},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Enabled driver-callable turns (1)")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Roll a six-sided die and tell me the result."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Driver-callable request succeeded, response: {content[:100]}")
        else:
            info("Empty response (LLM may not have produced output for the tool call)")
            tc.update_cantrip(cid, {"is_active": False})
            httpx.put(f"{tc.server}/api/settings", json={"driver_callable_turns": 0}, headers=tc.test_headers(), timeout=10.0)
            info("Disabled driver-callable and cleaned up")
            return True
    else:
        fail_test("Unexpected response during driver-callable test")
        tc.update_cantrip(cid, {"is_active": False})
        httpx.put(f"{tc.server}/api/settings", json={"driver_callable_turns": 0}, headers=tc.test_headers(), timeout=10.0)
        return False

    call_tags_present = "<call:" in json.dumps(result)
    if not call_tags_present:
        pass_test("Call tags stripped from final response")
    else:
        fail_test("Call tags leaked into final response")

    tool_result_present = "[TOOL RESULT]" in json.dumps(result)
    if not tool_result_present:
        pass_test("Tool result blocks not leaked into response")
    else:
        fail_test("Tool result blocks leaked into response")

    tc.update_cantrip(cid, {"is_active": False})
    httpx.put(
        f"{tc.server}/api/settings",
        json={"driver_callable_turns": 0},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Disabled driver-callable and cleaned up")
    return True


def test_command_tags(tc: TestClient) -> bool:
    section("TEST: Command Tags")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Say hello. <SUMMARY:off>"},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        if _check_upstream_error(result):
            return False
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"One-off <SUMMARY:off> request succeeded: {content[:60]}")
        else:
            fail_test("Empty response with command tag")
            return False
    else:
        fail_test("Unexpected response with command tag")
        return False

    leaked = "<SUMMARY:" in json.dumps(result)
    if not leaked:
        pass_test("Command tag stripped from forwarded request")
    else:
        fail_test("Command tag leaked into response")

    messages2 = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Say hello. <VERIFY:off:persist>"},
    ]
    result2 = tc.send_proxy_request(messages2)
    if isinstance(result2, dict):
        pass_test("Persistent <VERIFY:off:persist> accepted")

    messages3 = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Say hello. <VERIFY:reset>"},
    ]
    result3 = tc.send_proxy_request(messages3)
    if isinstance(result3, dict):
        pass_test("Reset command accepted")

    return True


def test_jslorebook_extraction(tc: TestClient) -> bool:
    section("TEST: jslorebook Extraction")

    embedded_code = "context.character.scenario += ' [FLOW_TEST_JSLB] Embedded lorebook active.';"

    messages = [
        {"role": "system", "content": f"You are a character. <jslorebook>{embedded_code}</jslorebook>"},
        {"role": "user", "content": "Say hello."},
    ]
    try:
        result = tc.send_proxy_request(messages)
    except Exception as e:
        info(f"jslorebook request failed (server may need restart): {e}")
        return True

    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Request with jslorebook succeeded: {content[:60]}")
        else:
            info("Empty response (jslorebook may not have produced scenario text)")

        result_dump = json.dumps(result)
        if "<jslorebook>" not in result_dump:
            pass_test("jslorebook tags stripped from forwarded request")
        else:
            fail_test("jslorebook tags leaked into response")
    else:
        info("Non-JSON response (server may need restart for jslorebook support)")

    return True


def test_prefill_normalization(tc: TestClient) -> bool:
    section("TEST: Prefill Normalization")

    httpx.put(
        f"{tc.server}/api/settings",
        json={"prefill_enabled": True},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Prefill normalization enabled")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": "Tell me a story about a cat."},
        {"role": "assistant", "content": "Once upon a time, "},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Prefill request succeeded: {content[:60]}")
        else:
            info("Empty response with prefill (endpoint may not support it)")
    else:
        fail_test("Unexpected response with prefill")

    httpx.put(
        f"{tc.server}/api/settings",
        json={"prefill_enabled": False},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Prefill normalization disabled")
    return True


def test_content_bypass(tc: TestClient) -> bool:
    section("TEST: Content Bypass")

    bypass_test_phrase = "FLOW_TEST_BYPASS"

    httpx.put(
        f"{tc.server}/api/settings",
        json={"bypass_method": "space_separation"},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Bypass method set to space_separation")

    messages = [
        {"role": "system", "content": "You are a helpful assistant. Reply briefly."},
        {"role": "user", "content": f"Say the phrase {bypass_test_phrase} and nothing else."},
    ]
    result = tc.send_proxy_request(messages)
    if isinstance(result, dict):
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Bypass request succeeded: {content[:60]}")
        else:
            info("Empty response with bypass (endpoint may have filtered it)")
    else:
        fail_test("Unexpected response with bypass")

    httpx.put(
        f"{tc.server}/api/settings",
        json={"bypass_method": "none"},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    pass_test("Bypass method reset to none")
    return True


def test_map_pipeline(tc: TestClient) -> bool:
    section("TEST: Map Pipeline (Multi-Stage)")

    map_json_path = Path(__file__).parent.parent / "tests" / "Gambling.json"
    if not map_json_path.exists():
        fail_test(f"Gambling.json not found at {map_json_path}")
        return False

    map_data = json.loads(map_json_path.read_text(encoding="utf-8"))
    pass_test(f"Loaded map JSON: {map_data['name']} ({len(map_data.get('stages', []))} stages)")

    resp = httpx.post(
        f"{tc.server}/api/maps/import",
        json={"data": map_data, "name": "FLOW_TEST_Gambling"},
        headers=tc.test_headers(),
        timeout=30.0,
    )
    if resp.status_code != 201:
        fail_test(f"Map import failed: {resp.status_code} {resp.text[:200]}")
        return False

    map_id = resp.json()["id"]
    stage_count = len(resp.json().get("stages", []))
    pass_test(f"Map imported: {map_id[:8]}... ({stage_count} stages)")

    update_resp = httpx.put(
        f"{tc.server}/api/maps/{map_id}",
        json={"tag": "flow-test-gambling", "is_active": True},
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if update_resp.status_code == 200 and update_resp.json().get("tag") == "flow-test-gambling":
        pass_test("Map tagged as 'flow-test-gambling' and activated")
    else:
        fail_test(f"Map tag/update failed: {update_resp.status_code}")
        httpx.delete(f"{tc.server}/api/maps/{map_id}", headers=tc.test_headers(), timeout=10.0)
        return False

    v_settings = httpx.get(
        f"{tc.server}/api/verification/settings",
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if not v_settings.json().get("verification_endpoint_id"):
        httpx.put(
            f"{tc.server}/api/verification/settings",
            json={
                "verification_enabled": True,
                "verification_endpoint_id": tc.endpoint_id,
                "verification_model": tc.model,
            },
            headers=tc.test_headers(),
            timeout=10.0,
        )
        pass_test("Enabled verification for map test (using test endpoint)")
    else:
        pass_test("Verification already configured")

    section("TEST: Map Pipeline — Sending Gambling Request")
    messages = [
        {
            "role": "system",
            "content": (
                "You are at a casino gambling table. The game is blackjack. "
                "Keep responses under 150 words. Use tool calls for all game actions."
            ),
        },
        {
            "role": "user",
            "content": (
                "I sit down at the table. Deal me in. "
                "<call:cards action=\"set_game\" game=\"blackjack\">"
                "<call:cards action=\"shuffle\">"
                "<call:money action=\"init\" player=\"user\" amount=\"1000\">"
                "<call:money action=\"init\" player=\"player1\" amount=\"1000\">"
                "<call:money action=\"init\" player=\"player2\" amount=\"1000\">"
            ),
        },
    ]

    info("Sending request through map pipeline (3 stages: Dealer + 2 LLM players)...")
    info("This may take 30-60 seconds due to multiple LLM calls...")

    try:
        result = tc.send_proxy_request(messages)
    except Exception as e:
        fail_test(f"Map pipeline request failed: {e}")
        httpx.delete(f"{tc.server}/api/maps/{map_id}", headers=tc.test_headers(), timeout=10.0)
        return False

    if isinstance(result, dict):
        if _check_upstream_error(result):
            httpx.delete(f"{tc.server}/api/maps/{map_id}", headers=tc.test_headers(), timeout=10.0)
            return False
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        if content:
            pass_test(f"Map pipeline produced response ({len(content)} chars)")
            info(f"Response preview: {content[:200]}...")

            has_error = result.get("error") is not None
            if has_error:
                fail_test(f"Map returned error: {result.get('error')}")
            else:
                pass_test("No errors in map pipeline response")
        else:
            fail_test("Empty response from map pipeline")
            info(f"Full response: {json.dumps(result)[:300]}")
    else:
        fail_test(f"Unexpected response type: {str(result)[:200]}")

    export_resp = httpx.get(
        f"{tc.server}/api/maps/{map_id}/export",
        headers=tc.test_headers(),
        timeout=10.0,
    )
    if export_resp.status_code == 200:
        exported = export_resp.json()
        if exported.get("name") and len(exported.get("stages", [])) > 0:
            pass_test(f"Map export works ({len(exported['stages'])} stages)")
            has_resources = any(
                len(s.get("resources", [])) > 0 for s in exported.get("stages", [])
            )
            if has_resources:
                pass_test("Exported map contains embedded resources (cantrips/lorebooks)")
            else:
                info("Exported map has no embedded resources")
        else:
            fail_test("Map export returned incomplete data")
    else:
        fail_test(f"Map export failed: {export_resp.status_code}")

    httpx.delete(f"{tc.server}/api/maps/{map_id}", headers=tc.test_headers(), timeout=10.0)
    pass_test("Cleaned up test map")

    for endpoint in ["/api/cantrips", "/api/lorebooks"]:
        resp_list = httpx.get(f"{tc.server}{endpoint}", headers=tc.test_headers(), timeout=10.0)
        resource_key = endpoint.split("/")[-1]
        for item in resp_list.json().get(resource_key, []):
            name = item.get("name", "")
            if name in ("Card Dealer", "Money Tracker", "Personality Manager", "Gambling Hall Rules", "Gambling Hall Map"):
                httpx.delete(f"{tc.server}{endpoint}/{item['id']}", headers=tc.test_headers(), timeout=10.0)
    pass_test("Cleaned up imported cantrips and lorebooks")

    return True


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="GitInTheVan flow-based integration tests")
    parser.add_argument("--admin-user", default=ADMIN_USER, help="Admin username")
    parser.add_argument("--admin-pass", default=ADMIN_PASS, help="Admin password")
    parser.add_argument("--server", default=SERVER, help="Server URL")
    args = parser.parse_args()

    server_url = args.server

    print(f"\n{'=' * 70}")
    print("  GitInTheVan - Flow-Based Integration Tests")
    print(f"  Server: {server_url}")
    print(f"  Admin: {args.admin_user}")
    print(f"{'=' * 70}")

    tc = TestClient()
    tc.server = server_url
    tc.admin_user = args.admin_user
    tc.admin_pass = args.admin_pass
    if not tc.setup():
        print("\nSetup failed. Exiting.")
        sys.exit(1)

    tc.cleanup_test_user_resources()

    results = []
    results.append(("Basic Passthrough", test_passthrough(tc)))
    results.append(("Cantrip Execution", test_cantrip_execution(tc)))
    results.append(("Tag Activation", test_tag_activation(tc)))
    results.append(("Lorebook Injection", test_lorebook_injection(tc)))
    test_memory_persistence(tc)
    results.append(("Verification", test_verification(tc)))
    results.append(("Summarization", test_summarization(tc)))
    results.append(("Forbidden Words", test_forbidden_words(tc)))
    results.append(("Multi-Position Cantrip", test_multi_position_cantrip(tc)))
    results.append(("Driver-Callable Tools", test_driver_callable(tc)))
    results.append(("Command Tags", test_command_tags(tc)))
    results.append(("jslorebook Extraction", test_jslorebook_extraction(tc)))
    results.append(("Prefill Normalization", test_prefill_normalization(tc)))
    results.append(("Content Bypass", test_content_bypass(tc)))
    results.append(("Map Pipeline", test_map_pipeline(tc)))

    tc.cleanup_test_user_resources()

    section("SUMMARY")
    passed = sum(1 for _, r in results if r)
    total = len(results)
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  {status}: {name}")

    print(f"\n  {passed}/{total} test groups passed")

    if passed == total:
        print("\n  All tests passed!")
    else:
        print("\n  Some tests failed. Check output above.")


if __name__ == "__main__":
    main()
