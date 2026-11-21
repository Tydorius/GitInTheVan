"""Regression tests for 0.45.1: map turns delivering hidden response tags to
Post-Navigator cantrips, and macro expansion leaving unknown {{user}} intact
while ignoring bare ``Persona:`` trait lines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.chat_data import ChatData
from app.services.deno_runner import DENO_PATH
from tests.conftest import TestSessionLocal


@pytest.mark.skipif(not Path(DENO_PATH).exists(), reason="Deno unavailable")
@pytest.mark.asyncio
async def test_map_turn_delivers_hidden_response_tags_to_post_navigator_cantrip(
    admin_client, httpx_mock
):
    client, _, api_key = admin_client

    ep_resp = await client.post(
        "/api/endpoints",
        json={
            "name": "MapEp",
            "base_url": "http://map-postnav.test",
            "api_key": "sk-map",
            "role_tag": "mapep",
            "priority": 1,
        },
    )
    assert ep_resp.status_code in (200, 201), ep_resp.text

    # Register a Post-Navigator cantrip that captures <deed>...</deed> into chat_data.
    c_resp = await client.post(
        "/api/cantrips",
        json={
            "name": "MapDeedCatcher",
            "is_active": True,
            "run_pre_driver": False,
            "run_post_navigator": True,
            "response_tags": ["deed"],
            "code": (
                "const deeds = (context.response_tags && context.response_tags.deed) || [];\n"
                "if (deeds.length > 0) {\n"
                "  context.chat_data.set('captured_map_deed', deeds[0]);\n"
                "}\n"
            ),
        },
    )
    assert c_resp.status_code in (200, 201), c_resp.text

    # Create a 1-stage map and set it as default_map_id.
    m_resp = await client.post(
        "/api/maps",
        json={
            "name": "DeedMap",
            "is_active": True,
            "stages": [
                {
                    "stage_order": 1,
                    "name": "Narrator",
                    "system_instructions": "Narrate the scene.",
                    "endpoint_tag": "mapep",
                    "output_mode": "persist",
                    "resources": [],
                }
            ],
        },
    )
    assert m_resp.status_code in (200, 201), m_resp.text
    map_id = m_resp.json()["id"]

    s_resp = await client.put("/api/settings", json={"default_map_id": map_id})
    assert s_resp.status_code == 200, s_resp.text

    httpx_mock.add_response(
        url="http://map-postnav.test/v1/chat/completions",
        json={
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "The door swings open. <deed>opened_vault</deed>",
                    }
                }
            ]
        },
    )

    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [
                {"role": "system", "content": "You are a narrator."},
                {"role": "user", "content": "I unlock the vault."},
            ],
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Chat-Id": "map-deed-chat-1",
        },
    )
    assert resp.status_code == 200, resp.text
    content = resp.json()["choices"][0]["message"]["content"]
    assert "<deed>" not in content
    assert "opened_vault" not in content
    assert "The door swings open." in content

    uid = (await client.get("/api/auth/me")).json()["id"]
    async with TestSessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatData).where(
                    ChatData.user_id == uid,
                    ChatData.key == "captured_map_deed",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert json.loads(rows[0].value_json) == "opened_vault"
