"""End-to-end check of the published Overthink Writing Pipeline maps.

Reads the map JSON straight out of the GitInTheVan-Public checkout and drives it
through the real proxy, so the shipped example and the pipeline cannot drift
apart silently. Skipped when that checkout is not present.
"""
import json
from pathlib import Path

import pytest

PUBLIC_REPO = Path(r"E:\github\GitInTheVan-Public")
MAP_4 = PUBLIC_REPO / "maps" / "overthink_writing_pipeline.json"
MAP_3 = PUBLIC_REPO / "maps" / "overthink_writing_pipeline_3stage.json"

pytestmark = pytest.mark.skipif(
    not MAP_4.exists(), reason="GitInTheVan-Public checkout not present"
)


async def _endpoint(client, name, host, tag):
    resp = await client.post("/api/endpoints", json={
        "name": name, "base_url": f"http://{host}", "api_key": f"sk-{name.lower()}",
        "role_tag": tag, "priority": 1,
    })
    assert resp.status_code in (200, 201), resp.text


def _body_to(httpx_mock, host):
    for r in httpx_mock.get_requests():
        if r.url.host == host:
            return json.dumps(json.loads(r.content))
    return ""


@pytest.mark.asyncio
async def test_published_four_stage_map_imports_and_runs(admin_client, httpx_mock):
    client, _, api_key = admin_client

    await client.put("/api/admin/settings", json={"max_map_stages": 4})
    await _endpoint(client, "Planner", "planner.test", "planner")
    await _endpoint(client, "Drafter", "drafter.test", "drafter")
    await _endpoint(client, "Critic", "critic.test", "critic")

    data = json.loads(MAP_4.read_text(encoding="utf-8"))
    imported = await client.post("/api/maps/import", json={"data": data})
    assert imported.status_code == 201, imported.text
    m = imported.json()

    assert [s["name"] for s in m["stages"]] == [
        "Architect", "Prose", "Line Editor", "Final Pass"
    ]
    assert [s["endpoint_tag"] for s in m["stages"]] == [
        "planner", "drafter", "critic", "drafter"
    ]
    assert [s["output_mode"] for s in m["stages"]] == [
        "sanitize", "sanitize", "sanitize", "persist"
    ]

    skills = {s["name"]: s for s in (await client.get("/api/skills")).json()["skills"]}
    assert skills["Style Anchor"]["type"] == "sample"
    assert skills["LLMism Checklist"]["type"] == "skill"

    await client.put("/api/settings", json={"default_map_id": m["id"]})

    # The drafter tag serves stages 2 and 4, so it is called twice.
    httpx_mock.add_response(
        url="http://planner.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "THE PLAN"}}]},
    )
    httpx_mock.add_response(
        url="http://drafter.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "THE DRAFT"}}]},
    )
    httpx_mock.add_response(
        url="http://critic.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "THE EDITS"}}]},
    )
    httpx_mock.add_response(
        url="http://drafter.test/v1/chat/completions",
        json={"choices": [{"message": {"content": "THE FINISHED SCENE"}}]},
    )

    resp = await client.post("/v1/chat/completions", json={
        "model": "test",
        "messages": [{"role": "user", "content": "Write the scene where she leaves."}],
    }, headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "THE FINISHED SCENE"

    hosts = [r.url.host for r in httpx_mock.get_requests()]
    assert hosts == ["planner.test", "drafter.test", "critic.test", "drafter.test"]

    plan_body = _body_to(httpx_mock, "planner.test")
    critic_body = _body_to(httpx_mock, "critic.test")
    final_body = json.dumps(json.loads(
        [r for r in httpx_mock.get_requests() if r.url.host == "drafter.test"][-1].content
    ))

    # The planner is not handed the style anchor or the editing checklist.
    assert "Cliches I am refusing" in plan_body
    assert "writing_sample" not in plan_body
    assert "LLMism Checklist" not in plan_body

    # The editor sees the draft and its checklist, not the anchor.
    assert "[STAGE 2 OUTPUT: Prose]" in critic_body
    assert "Vocabulary tells" in critic_body
    assert "writing_sample" not in critic_body

    # The final pass sees all three prior outputs exactly once, plus the anchor.
    # Matched on wrapper + content, and on the sample's *closing* tag: the stage's
    # own instructions name the [STAGE N OUTPUT: ...] labels and the opening
    # <writing_sample> tag, so those bare markers legitimately appear twice.
    assert final_body.count("[STAGE 1 OUTPUT: Architect]\\nTHE PLAN") == 1
    assert final_body.count("[STAGE 2 OUTPUT: Prose]\\nTHE DRAFT") == 1
    assert final_body.count("[STAGE 3 OUTPUT: Line Editor]\\nTHE EDITS") == 1
    assert final_body.count("</writing_sample>") == 1
    assert "Vocabulary tells" not in final_body


@pytest.mark.asyncio
async def test_published_three_stage_map_runs_at_stock_cap(admin_client, httpx_mock):
    """The 3-stage variant must run without any admin change."""
    client, _, api_key = admin_client

    caps = (await client.get("/api/admin/settings")).json()
    assert caps["max_map_stages"] == 3, "stock cap assumption changed"

    await _endpoint(client, "Planner", "planner.test", "planner")
    await _endpoint(client, "Drafter", "drafter.test", "drafter")
    await _endpoint(client, "Critic", "critic.test", "critic")

    data = json.loads(MAP_3.read_text(encoding="utf-8"))
    imported = await client.post("/api/maps/import", json={"data": data})
    assert imported.status_code == 201, imported.text
    m = imported.json()
    assert [s["name"] for s in m["stages"]] == ["Architect", "Prose", "Editor & Polish"]

    await client.put("/api/settings", json={"default_map_id": m["id"]})

    for host, out in [
        ("planner.test", "PLAN"), ("drafter.test", "DRAFT"), ("critic.test", "POLISHED")
    ]:
        httpx_mock.add_response(
            url=f"http://{host}/v1/chat/completions",
            json={"choices": [{"message": {"content": out}}]},
        )

    resp = await client.post("/v1/chat/completions", json={
        "model": "test", "messages": [{"role": "user", "content": "Write a scene"}],
    }, headers={"Authorization": f"Bearer {api_key}"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["choices"][0]["message"]["content"] == "POLISHED"
    assert len([r for r in httpx_mock.get_requests()]) == 3
