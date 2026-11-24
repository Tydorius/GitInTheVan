from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from app.models.user import User
from app.models.user_settings import UserSettings
from app.services.debug import init_debug
from app.services.proxy import _forward_request_impl
from app.services.routing import FailoverEndpoint
from app.services.upstream import apply_auto_cache_control, forward_llm_chain
from tests.conftest import TestSessionLocal


def _make_starlette_request(body: dict[str, Any], path: str = "/v1/chat/completions") -> Request:
    raw = json.dumps(body).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": raw, "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    return Request(scope, receive)


async def _create_user_with_settings(
    username: str,
    *,
    cache_friendly_mode: bool = False,
    auto_cache_control: bool = False,
    debug_mode: bool = False,
) -> str:
    async with TestSessionLocal() as db:
        user = User(username=username, password_hash="x", gitv_api_key=f"gitv_{username}")
        db.add(user)
        await db.flush()
        us = UserSettings(
            user_id=user.id,
            cache_friendly_mode=cache_friendly_mode,
            auto_cache_control=auto_cache_control,
            debug_mode=debug_mode,
        )
        db.add(us)
        await db.commit()
        return user.id


# ---------------------------------------------------------------------------
# A2: apply_auto_cache_control unit tests
# ---------------------------------------------------------------------------


def test_apply_auto_cache_control_anthropic_stamps_two_breakpoints_without_mutating():
    original = [
        {"role": "system", "content": "You are a helpful guide."},
        {"role": "user", "content": "First question"},
        {"role": "assistant", "content": "First answer"},
        {"role": "user", "content": "Second question"},
    ]
    snapshot = copy.deepcopy(original)

    stamped, count = apply_auto_cache_control(
        original,
        provider="anthropic",
        model="claude-3-7-sonnet",
    )

    assert count == 2
    # Input never mutated
    assert original == snapshot
    # Last system message stamped
    assert stamped[0]["content"] == [
        {
            "type": "text",
            "text": "You are a helpful guide.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Second-to-last non-system message stamped (index 2: assistant)
    assert stamped[2]["content"] == [
        {
            "type": "text",
            "text": "First answer",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    # Other messages untouched
    assert stamped[1] == original[1]
    assert stamped[3] == original[3]


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("openrouter", "meta-llama/llama-3.3-70b", ""),
        ("", "gpt-4o", "https://openrouter.ai/api/v1"),
        ("", "custom-model", "https://api.anthropic.com/v1"),
        ("", "claude-3-5-sonnet-20241022", "http://localhost:4000"),
    ],
)
def test_apply_auto_cache_control_eligible_targets(provider: str, model: str, base_url: str):
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]
    stamped, count = apply_auto_cache_control(
        messages,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    assert count == 2
    assert stamped[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert stamped[2]["content"][0]["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize(
    ("provider", "model", "base_url"),
    [
        ("openai", "gpt-4o", "https://api.openai.com"),
        ("gemini", "gemini-2.5-pro", ""),
        ("openai_compatible", "llama-3.3-70b", "http://localhost:8080"),
        ("", "qwen2.5-32b", "http://127.0.0.1:11434"),
    ],
)
def test_apply_auto_cache_control_ineligible_targets_untouched(
    provider: str, model: str, base_url: str
):
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]
    stamped, count = apply_auto_cache_control(
        messages,
        provider=provider,
        model=model,
        base_url=base_url,
    )
    assert count == 0
    assert stamped is messages


def test_apply_auto_cache_control_multimodal_list_messages_without_mutating():
    original = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "Part A"},
                {"type": "text", "text": "Part B"},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                {"type": "text", "text": "Look at this"},
            ],
        },
        {"role": "user", "content": "Follow up"},
    ]
    snapshot = copy.deepcopy(original)

    stamped, count = apply_auto_cache_control(original, provider="anthropic")

    assert count == 2
    assert original == snapshot
    # Last text block in system message gets cache_control; first stays untouched
    assert "cache_control" not in stamped[0]["content"][0]
    assert stamped[0]["content"][1]["cache_control"] == {"type": "ephemeral"}
    # Last text block in second-to-last non-system message gets cache_control
    assert "cache_control" not in stamped[1]["content"][0]
    assert stamped[1]["content"][1]["cache_control"] == {"type": "ephemeral"}


def test_apply_auto_cache_control_skips_already_marked_messages():
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "System", "cache_control": {"type": "ephemeral"}}
            ],
        },
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]

    stamped, count = apply_auto_cache_control(messages, provider="anthropic")
    assert count == 1
    assert stamped[0] == messages[0]
    assert stamped[2]["content"] == [
        {"type": "text", "text": "A1", "cache_control": {"type": "ephemeral"}}
    ]

    # Running a second time adds 0 breakpoints
    stamped_again, count_again = apply_auto_cache_control(stamped, provider="anthropic")
    assert count_again == 0
    assert stamped_again is stamped


def test_apply_auto_cache_control_single_non_system_message_stamps_only_system():
    messages = [
        {"role": "system", "content": "System"},
        {"role": "user", "content": "Only turn"},
    ]
    stamped, count = apply_auto_cache_control(messages, provider="anthropic")
    assert count == 1
    assert stamped[0]["content"] == [
        {"type": "text", "text": "System", "cache_control": {"type": "ephemeral"}}
    ]
    assert stamped[1] == messages[1]


def test_apply_auto_cache_control_multiple_system_messages_stamps_last_system():
    messages = [
        {"role": "system", "content": "System 1"},
        {"role": "system", "content": "System 2"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]
    stamped, count = apply_auto_cache_control(messages, provider="openrouter")
    assert count == 2
    assert stamped[0] == messages[0]
    assert stamped[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert stamped[3]["content"][0]["cache_control"] == {"type": "ephemeral"}


# ---------------------------------------------------------------------------
# A2: forward_llm_chain per-candidate stamping & debug_capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_llm_chain_stamps_only_eligible_failover_candidate(monkeypatch):
    seen_bodies: list[tuple[str, dict[str, Any]]] = []

    async def fake_do_forward(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: httpx.Timeout,
        provider: str = "",
        base_url: str = "",
        api_key: str = "",
        configured: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        parsed = json.loads(body)
        seen_bodies.append((provider, parsed))
        if provider == "openai":
            return {"error": {"message": "upstream fail"}}, 502
        return {
            "id": "chatcmpl-ok",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
        }, 200

    monkeypatch.setattr("app.services.proxy._do_forward", fake_do_forward)

    original_msgs = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Turn 1"},
        {"role": "assistant", "content": "Reply 1"},
        {"role": "user", "content": "Turn 2"},
    ]
    hash_anchor = copy.deepcopy(original_msgs)
    body_json: dict[str, Any] = {
        "model": "gpt-4o",
        "messages": copy.deepcopy(original_msgs),
        "_gitv_messages_for_hash": hash_anchor,
        "_gitv_auto_cache_control": True,
    }
    init_debug(body_json, [])

    chain = [
        FailoverEndpoint(
            endpoint_id="ep-1",
            endpoint_name="OpenAI Primary",
            base_url="https://api.openai.com",
            api_key="sk-1",
            provider="openai",
            model="gpt-4o",
        ),
        FailoverEndpoint(
            endpoint_id="ep-2",
            endpoint_name="Anthropic Backup",
            base_url="https://api.anthropic.com",
            api_key="sk-2",
            provider="anthropic",
            model="claude-3-7-sonnet",
        ),
    ]

    body_bytes = json.dumps({k: v for k, v in body_json.items() if not k.startswith("_gitv")}).encode()
    resp, status = await forward_llm_chain(body_json, chain, body_bytes=body_bytes)

    assert status == 200
    assert resp["id"] == "chatcmpl-ok"
    assert len(seen_bodies) == 2

    # First candidate (OpenAI) got plain string messages, no cache_control
    assert seen_bodies[0][0] == "openai"
    assert seen_bodies[0][1]["messages"][0]["content"] == "System prompt"
    assert seen_bodies[0][1]["messages"][2]["content"] == "Reply 1"

    # Second candidate (Anthropic) got 2 cache_control breakpoints
    assert seen_bodies[1][0] == "anthropic"
    assert seen_bodies[1][1]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen_bodies[1][1]["messages"][2]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # Internal _gitv_* keys never leaked
    for _, payload in seen_bodies:
        assert not any(k.startswith("_gitv") for k in payload)

    # Shared body_json["messages"] and _gitv_messages_for_hash were never mutated
    assert body_json["messages"] == original_msgs
    assert body_json["_gitv_messages_for_hash"] == original_msgs

    # Debug capture emitted for cache_control
    stages = body_json["_gitv_debug"]["stages"]
    cc_stages = [s for s in stages if s["name"] == "cache_control"]
    assert len(cc_stages) == 1
    assert cc_stages[0]["metadata"] == {
        "breakpoints": 2,
        "provider": "anthropic",
        "model": "claude-3-7-sonnet",
    }


# ---------------------------------------------------------------------------
# A1: proxy.py memory injection (cache_friendly_mode True vs False)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proxy_memory_injection_cache_friendly_mode_puts_sorted_memory_in_user_tail(
    monkeypatch,
):
    user_id = await _create_user_with_settings(
        "cache_friendly_user",
        cache_friendly_mode=True,
        auto_cache_control=True,
        debug_mode=True,
    )

    captured_wire: dict[str, Any] = {}
    captured_debug_stages: list[dict[str, Any]] = []

    async def fake_load_memories(chat_id: str, uid: str) -> dict[str, str]:
        # Intentionally unsorted keys
        return {"zeta": "last", "alpha": "first", "mid": "middle"}

    async def fake_do_forward(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: httpx.Timeout,
        provider: str = "",
        base_url: str = "",
        api_key: str = "",
        configured: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        captured_wire.update(json.loads(body))
        return {
            "id": "chatcmpl-cf",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done"}}],
        }, 200

    async def fake_finalize(response_data, status_code, body_json, *args, **kwargs):
        captured_debug_stages.extend(body_json.get("_gitv_debug", {}).get("stages", []))
        return response_data

    monkeypatch.setattr("app.services.conversation.load_memories_for_chat", fake_load_memories)
    monkeypatch.setattr("app.services.proxy._do_forward", fake_do_forward)
    monkeypatch.setattr("app.services.proxy.finalize_response", fake_finalize)

    req_body = {
        "model": "claude-3-7-sonnet",
        "messages": [
            {"role": "system", "content": "Static system prompt."},
            {"role": "user", "content": "Earlier message"},
            {"role": "assistant", "content": "Earlier reply"},
            {"role": "user", "content": "Current user prompt"},
        ],
        "stream": False,
    }
    request = _make_starlette_request(req_body)
    target = (
        "https://api.anthropic.com",
        "sk-test",
        user_id,
        "",
        "none",
        "anthropic",
        "claude-3-7-sonnet",
        "",
        [],
    )

    resp = await _forward_request_impl(request, target=target)
    assert resp.status_code == 200

    # No _gitv_* internal keys leaked onto the wire
    assert not any(k.startswith("_gitv") for k in captured_wire)

    msgs = captured_wire["messages"]
    # messages[0] (system) must NOT contain [PERSISTENT MEMORY]
    sys_content = msgs[0]["content"]
    sys_text = sys_content[0]["text"] if isinstance(sys_content, list) else sys_content
    assert sys_text == "Static system prompt."
    assert "[PERSISTENT MEMORY]" not in sys_text
    # auto_cache_control also stamped the system message and second-to-last non-system message
    assert isinstance(sys_content, list)
    assert sys_content[0]["cache_control"] == {"type": "ephemeral"}
    assert msgs[2]["content"][0]["cache_control"] == {"type": "ephemeral"}

    # Newest user message carries deterministically sorted memory block
    newest_user = msgs[-1]["content"]
    assert newest_user.startswith("Current user prompt\n\n[PERSISTENT MEMORY]\n")
    assert newest_user.index("alpha: first") < newest_user.index("mid: middle") < newest_user.index("zeta: last")

    # Debug capture records position="user_tail" and sorted memory_keys
    mem_stages = [s for s in captured_debug_stages if s["name"] == "memory_injection"]
    assert len(mem_stages) == 1
    assert mem_stages[0]["metadata"]["position"] == "user_tail"
    assert mem_stages[0]["metadata"]["memory_keys"] == ["alpha", "mid", "zeta"]


@pytest.mark.asyncio
async def test_proxy_memory_injection_legacy_mode_keeps_system_message_behavior(monkeypatch):
    user_id = await _create_user_with_settings(
        "legacy_mem_user",
        cache_friendly_mode=False,
        auto_cache_control=False,
        debug_mode=True,
    )

    captured_wire: dict[str, Any] = {}
    captured_debug_stages: list[dict[str, Any]] = []

    async def fake_load_memories(chat_id: str, uid: str) -> dict[str, str]:
        return {"b_key": "two", "a_key": "one"}

    async def fake_do_forward(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout: httpx.Timeout,
        provider: str = "",
        base_url: str = "",
        api_key: str = "",
        configured: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], int]:
        captured_wire.update(json.loads(body))
        return {
            "id": "chatcmpl-leg",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done"}}],
        }, 200

    async def fake_finalize(response_data, status_code, body_json, *args, **kwargs):
        captured_debug_stages.extend(body_json.get("_gitv_debug", {}).get("stages", []))
        return response_data

    monkeypatch.setattr("app.services.conversation.load_memories_for_chat", fake_load_memories)
    monkeypatch.setattr("app.services.proxy._do_forward", fake_do_forward)
    monkeypatch.setattr("app.services.proxy.finalize_response", fake_finalize)

    req_body = {
        "model": "claude-3-7-sonnet",
        "messages": [
            {"role": "system", "content": "Static system prompt."},
            {"role": "user", "content": "Current user prompt"},
        ],
        "stream": False,
    }
    request = _make_starlette_request(req_body)
    target = (
        "https://api.anthropic.com",
        "sk-test",
        user_id,
        "",
        "none",
        "anthropic",
        "claude-3-7-sonnet",
        "",
        [],
    )

    resp = await _forward_request_impl(request, target=target)
    assert resp.status_code == 200

    msgs = captured_wire["messages"]
    # Legacy mode injects memory into messages[0] (system message), sorted deterministically
    assert isinstance(msgs[0]["content"], str)
    assert msgs[0]["content"].startswith("Static system prompt.\n\n[PERSISTENT MEMORY]\n")
    assert msgs[0]["content"].index("a_key: one") < msgs[0]["content"].index("b_key: two")
    # Newest user message untouched
    assert msgs[-1]["content"] == "Current user prompt"
    # Debug metadata records system_start
    mem_stages = [s for s in captured_debug_stages if s["name"] == "memory_injection"]
    assert len(mem_stages) == 1
    assert mem_stages[0]["metadata"]["position"] == "system_start"
    assert mem_stages[0]["metadata"]["memory_keys"] == ["a_key", "b_key"]
