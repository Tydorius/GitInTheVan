"""Integration test for the full proxy loop: client -> proxy -> LLM -> client.

Tests passthrough, lorebook injection, script execution, and streaming
against a real LLM endpoint. Prints detailed output for manual review.

Usage:
    .venv/Scripts/python.exe scripts/integration_test.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx

PROXY_URL = "http://127.0.0.1:8000"
ENDPOINT_URL = "https://owui.trustastranger.com"
API_KEY = "sk-92469c45d3a547dcbdc3acda4852a8a2"
API_BASE_PATH = "/api"
MODEL = "Gemma-4-31B-it"

ADMIN_USER = "admin"
ADMIN_PASS = "adminpass123"

LINE = "=" * 70


def section(title: str) -> None:
    print(f"\n{LINE}\n  {title}\n{LINE}")


async def wait_for_proxy(client: httpx.AsyncClient) -> bool:
    try:
        resp = await client.get(f"{PROXY_URL}/health")
        return resp.status_code == 200
    except Exception:
        return False


async def setup_admin(client: httpx.AsyncClient) -> tuple[str, str]:
    """Setup admin or login. Returns (token, gitv_api_key)."""
    setup = await client.post(
        f"{PROXY_URL}/api/auth/setup",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
    )
    if setup.status_code == 201:
        data = setup.json()
        return data["access_token"], data["api_key"]

    login = await client.post(
        f"{PROXY_URL}/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
    )
    token = login.json()["access_token"]

    users = await client.get(
        f"{PROXY_URL}/api/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    gitv_key = None
    for u in users.json().get("users", []):
        if u.get("username") == ADMIN_USER:
            gitv_key = u.get("gitv_api_key")

    if not gitv_key:
        raise RuntimeError("Could not retrieve gitv_ API key")

    return token, gitv_key


async def test_passthrough(client: httpx.AsyncClient, gitv_key: str) -> None:
    """Test: basic proxy passthrough using gitv_ API key."""
    section("STEP 1: Proxy Passthrough (no scripts/lorebooks)")

    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a fantasy character named Aria. Stay in character. Reply in 2-3 sentences.",
            },
            {"role": "user", "content": "Hello, who are you?"},
        ],
        "max_tokens": 500,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {gitv_key}", "Content-Type": "application/json"}

    print(f"\n  Sending through proxy -> {ENDPOINT_URL}{API_BASE_PATH}/chat/completions")
    print(f"  Model: {MODEL}")

    start = time.time()
    resp = await client.post(f"{PROXY_URL}/v1/chat/completions", json=body, headers=headers)
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code}  Elapsed: {elapsed:.1f}s")
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  Response:\n    {content[:400]}")
    else:
        print(f"  ERROR: {resp.text[:500]}")


async def test_default_endpoint_passthrough(client: httpx.AsyncClient) -> None:
    """Test: passthrough using the default endpoint (no gitv_ key needed)."""
    section("STEP 1b: Default Endpoint Passthrough (no API key routing)")

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "Reply briefly in one sentence."},
            {"role": "user", "content": "What is 2+2?"},
        ],
        "max_tokens": 50,
        "stream": False,
    }
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

    print("\n  Sending with raw API key (default endpoint fallback)")

    start = time.time()
    resp = await client.post(f"{PROXY_URL}/v1/chat/completions", json=body, headers=headers)
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code}  Elapsed: {elapsed:.1f}s")
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  Response: {content[:200]}")
    else:
        print(f"  ERROR: {resp.text[:500]}")


async def test_script_execution(
    client: httpx.AsyncClient, token: str, gitv_key: str
) -> None:
    """Test: script modifies request, LLM responds to injected lore."""
    section("STEP 2: Script Execution Through Proxy")

    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    script_code = """
context.character = context.character || {};
context.character.scenario = context.character.scenario || "";
context.character.personality = context.character.personality || "";

const lastMessage = context.chat.last_message.toLowerCase();
if (lastMessage.includes("tavern")) {
    context.character.scenario += " The tavern is called 'The Rusty Anchor'. It is filled with sailors drinking rum. A mysterious hooded figure sits alone in the corner, watching everyone. The barkeep is a grizzled dwarf named Brom.";
    context.character.personality += ", wary of the hooded stranger in the corner";
    console.log("Tavern lore injected: Rusty Anchor, hooded figure, Brom the barkeep");
} else {
    context.character.scenario += " The sun is setting over the medieval town of Eldoria.";
    console.log("Generic scene injected: sunset in Eldoria");
}
""".strip()

    print("\n  Creating script...")
    resp = await client.post(
        f"{PROXY_URL}/api/scripts",
        json={"name": "Tavern Lore", "code": script_code, "is_active": True},
        headers=auth,
    )
    print(f"  Script created: {resp.status_code}")

    print("\n  Testing script in isolation (no LLM call):")
    test_resp = await client.post(
        f"{PROXY_URL}/api/scripts/test",
        json={
            "code": script_code,
            "messages": [{"role": "user", "content": "I walk into the tavern"}],
            "character_name": "Aria",
        },
        headers=auth,
    )
    if test_resp.status_code == 200:
        result = test_resp.json()
        print(f"    Scenario: {result['scenario'][:200]}")
        print(f"    Personality: {result['personality'][:200]}")
        print(f"    Debug: {result['debug_logs']}")
        print(f"    Error: {result['error']}")

    print("\n  Sending 'tavern' request through proxy to LLM:")
    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a fantasy character named Aria. Stay in character. Reply in 2-3 sentences.",
            },
            {"role": "user", "content": "I walk into the tavern and look around."},
        ],
        "max_tokens": 600,
        "stream": False,
    }
    proxy_headers = {"Authorization": f"Bearer {gitv_key}", "Content-Type": "application/json"}

    start = time.time()
    resp = await client.post(
        f"{PROXY_URL}/v1/chat/completions", json=body, headers=proxy_headers
    )
    elapsed = time.time() - start

    print(f"  Status: {resp.status_code}  Elapsed: {elapsed:.1f}s")
    if resp.status_code == 200:
        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        print(f"  LLM Response:\n    {content[:500]}")
        print("\n  Check: Does the response mention tavern details (Rusty Anchor,")
        print("  hooded figure, Brom)? The script injected this lore before forwarding.")
    else:
        print(f"  ERROR: {resp.text[:500]}")


async def test_streaming(client: httpx.AsyncClient, gitv_key: str) -> None:
    """Test: SSE streaming passthrough."""
    section("STEP 3: Streaming Passthrough")

    body = {
        "model": MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are a storyteller. Tell a very short story (3-4 sentences).",
            },
            {"role": "user", "content": "Tell me about a dragon who collects teacups."},
        ],
        "max_tokens": 500,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {gitv_key}", "Content-Type": "application/json"}

    print(f"\n  Streaming from proxy -> LLM")
    print("  First 5 chunks:")

    start = time.time()
    chunk_count = 0
    full_text = ""

    async with client.stream(
        "POST", f"{PROXY_URL}/v1/chat/completions", json=body, headers=headers
    ) as resp:
        print(f"  Status: {resp.status_code}")
        if resp.status_code != 200:
            body_bytes = await resp.aread()
            print(f"  ERROR: {body_bytes.decode()[:300]}")
            return

        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    print("\n  [DONE]")
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        chunk_count += 1
                        full_text += delta
                        if chunk_count <= 5:
                            print(f"    [{chunk_count}] \"{delta}\"")
                        elif chunk_count == 6:
                            print("    ... (streaming)")
                except json.JSONDecodeError:
                    pass

    elapsed = time.time() - start
    print(f"\n  Total chunks: {chunk_count}")
    print(f"  Full text ({len(full_text)} chars):\n    {full_text[:400]}")
    print(f"  Elapsed: {elapsed:.1f}s")


async def test_script_isolation(client: httpx.AsyncClient, token: str) -> None:
    """Test: script test endpoint with chat_data persistence simulation."""
    section("STEP 4: Script Test Endpoint (no LLM, verifying sandbox)")

    auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    script_code = """
context.character.scenario += " A storm rages outside. Lightning illuminates the room.";
context.character.personality += ", tense from the thunder";

const day = context.chat_data.get('day') || 1;
context.chat_data.set('day', day + 1);
context.character.scenario += " [Day " + day + "]";

console.log("Storm script executed. Day: " + (day + 1));
console.log("User: " + context.chat.user_name);
console.log("Character: " + context.character.name);
""".strip()

    print("\n  Testing with simulated chat_data:")
    resp = await client.post(
        f"{PROXY_URL}/api/scripts/test",
        json={
            "code": script_code,
            "messages": [{"role": "user", "content": "I sit by the fireplace"}],
            "character_name": "Aria",
            "user_name": "Tydorius",
            "conversation_id": "storm-test",
            "chat_data": {"day": 7},
        },
        headers=auth,
    )
    if resp.status_code == 200:
        result = resp.json()
        print(f"    Scenario: {result['scenario']}")
        print(f"    Personality: {result['personality']}")
        print(f"    chat_data: {json.dumps(result['chat_data'])}")
        print(f"    Debug logs: {result['debug_logs']}")
        print(f"    Error: {result['error']}")
        print("\n  Expected: Day=8, storm scenario, Tydorius+Aria in logs")
    else:
        print(f"  FAILED: {resp.status_code} {resp.text[:300]}")


async def main() -> None:
    print(LINE)
    print("  GitInTheVan - Full Integration Test")
    print(f"  Endpoint: {ENDPOINT_URL}{API_BASE_PATH}/chat/completions")
    print(f"  Model: {MODEL}")
    print(LINE)

    timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if not await wait_for_proxy(client):
            print(f"\nProxy not running at {PROXY_URL}")
            print("Start with: .venv/Scripts/uvicorn app.main:app")
            sys.exit(1)
        print("Proxy health: OK")

        section("SETUP: Admin + Endpoint Configuration")
        token, gitv_key = await setup_admin(client)
        print(f"  Admin token: {token[:20]}...")
        print(f"  gitv API key: {gitv_key}")

        auth = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        existing = await client.get(f"{PROXY_URL}/api/endpoints", headers=auth)
        endpoints = existing.json().get("endpoints", [])
        if endpoints:
            ep_id = endpoints[0]["id"]
            print(f"  Using existing endpoint: {ep_id[:8]}...")
        else:
            ep_resp = await client.post(
                f"{PROXY_URL}/api/endpoints",
                json={
                    "name": "OpenWebUI Test",
                    "base_url": ENDPOINT_URL,
                    "api_key": API_KEY,
                    "api_base_path": API_BASE_PATH,
                    "enabled": True,
                },
                headers=auth,
            )
            ep_id = ep_resp.json()["id"]
            print(f"  Endpoint created: {ep_id[:8]}...")

        await client.put(
            f"{PROXY_URL}/api/settings",
            json={"default_endpoint_id": ep_id},
            headers=auth,
        )
        print("  Default endpoint set.")

        await test_passthrough(client, gitv_key)
        await test_script_execution(client, token, gitv_key)
        await test_streaming(client, gitv_key)
        await test_script_isolation(client, token)

    section("Integration Test Complete")
    print("  Review outputs above.")


if __name__ == "__main__":
    asyncio.run(main())
