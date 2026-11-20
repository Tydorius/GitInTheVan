"""0.42.0 budget fixes (B9a, B9b) — the KoK report's budget findings.

B9a: the budget's resource set counted every skill and writing sample the
user OWNS, not only the ones attached to the endpoint in use, diluting
every other resource's share.

B9b: lorebook text was injected before the budget was computed, so
injected text was counted as conversation and the lorebook share -- which
match_entries has supported since Phase 12 -- was never passed by any
caller. The proxy now computes the budget first and caps each book's
matching with its share.
"""

from __future__ import annotations

import json

import pytest

MOCK_BUDGET_URL = "http://mock-budget:9999"


def _mock_reply() -> dict:
    return {
        "id": "t", "object": "chat.completion", "created": 1, "model": "m",
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
    }


class TestEndpointScopedSkills:
    @pytest.mark.asyncio
    async def test_only_attached_skills_count(self, admin_client):
        """A skill attached to the endpoint counts; an unattached one must
        not dilute the shares."""
        client, _, _ = admin_client
        uid = (await client.get("/api/auth/me")).json()["id"]

        ep = await client.post("/api/endpoints", json={
            "name": "BudgetEP", "base_url": MOCK_BUDGET_URL, "api_key": "sk-test",
        })
        ep_id = ep.json()["id"]

        attached = await client.post("/api/skills", json={
            "name": "Attached", "type": "skill", "content": "be vivid",
        })
        attached_id = attached.json()["id"]
        attach = await client.post(f"/api/skills/{attached_id}/attach", json={
            "endpoint_id": ep_id,
        })
        assert attach.status_code == 201, attach.text
        await client.post("/api/skills", json={
            "name": "Orphan", "type": "skill", "content": "never injected",
        })

        from app.services.budget import prepare_budget

        body = {"model": "m", "stream": False,
                "messages": [{"role": "user", "content": "hi"}]}
        allocations = await prepare_budget(body, uid, [], endpoint_id=ep_id)
        assert allocations is not None
        ids = set(allocations["allocations"].keys())
        assert attached_id in ids
        orphans = [
            a for a in allocations["allocations"].values()
            if a.get("name") == "Orphan"
        ]
        assert not orphans, "unattached skills must not count (B9a)"


class TestBudgetCapsLorebookInjection:
    @pytest.mark.asyncio
    async def test_book_share_caps_matching(self, admin_client, httpx_mock):
        """E2E: with a 1% injection budget (default 8192 window -> ~81
        tokens -> ~326 chars for the lone book), the first entry lands and
        the cap stops everything after it. Before B9b no cap was passed at
        all and every matching entry injected unbounded."""
        client, _, gitv_key = admin_client

        ep = await client.post("/api/endpoints", json={
            "name": "BudgetE2E", "base_url": MOCK_BUDGET_URL, "api_key": "sk-test",
        })
        await client.put("/api/settings", json={
            "default_endpoint_id": ep.json()["id"],
            "context_budget_percent": 1,
        })
        try:
            lb = await client.post("/api/lorebooks", json={
                "name": "HugeBook", "tag": "", "is_active": True,
            })
            lb_id = lb.json()["id"]
            await client.post(f"/api/lorebooks/{lb_id}/entries", json={
                "name": "WallA", "keys": ["triggerword"],
                "content": "A" * 4000,
                "position": "before_last_message", "insertion_order": 1,
                "is_constant": False, "is_selective": False,
            })
            await client.post(f"/api/lorebooks/{lb_id}/entries", json={
                "name": "WallB", "keys": ["triggerword"],
                "content": "B" * 4000,
                "position": "before_last_message", "insertion_order": 2,
                "is_constant": False, "is_selective": False,
            })

            httpx_mock.add_response(
                url=f"{MOCK_BUDGET_URL}/v1/chat/completions",
                json=_mock_reply(), status_code=200,
            )
            resp = await client.post(
                "/v1/chat/completions",
                json={"model": "m", "stream": False,
                      "messages": [{"role": "user", "content": "triggerword"}]},
                headers={"Authorization": f"Bearer {gitv_key}"},
            )
            assert resp.status_code == 200, resp.text

            forwarded = httpx_mock.get_requests()[-1]
            text = json.dumps(json.loads(forwarded.content)["messages"])
            assert "A" * 4000 in text, "the first entry still injects"
            assert "B" * 4000 not in text, (
                "the book's budget share must cap further entries (B9b)"
            )
        finally:
            await client.put("/api/settings", json={"context_budget_percent": 10})
