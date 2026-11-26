"""v0.51.1 backend housekeeping tests.

Covers:
1. Removal of dormant kwargs from ``forward_llm_chain()``.
2. ``apply_history_ops()`` only counting ops that actually mutate ``messages``.
3. ``regex_pattern`` validation when restoring ``verification_rule`` snapshots.
"""

from __future__ import annotations

import copy
import inspect
import json

from sqlalchemy import select

from app.models.resource_snapshot import ResourceSnapshot
from app.services.cantrip import apply_history_ops
from app.services.debug import init_debug
from app.services.upstream import forward_llm_chain
from tests.conftest import TestSessionLocal


class TestForwardLlmChainSignature:
    def test_dormant_kwargs_removed(self):
        params = set(inspect.signature(forward_llm_chain).parameters)
        assert "extra_layers_fn" not in params
        assert "default_params" not in params
        assert "base_messages" not in params


class TestHistoryOpsNoopAccounting:
    def test_keep_recent_noop_does_not_increment_ops_applied(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        body = {"messages": msgs, "_gitv_history_ops": [{"op": "keep_recent", "count": 10}]}
        init_debug(body, [])

        summary = apply_history_ops(body)

        assert summary == {"ops_applied": 0, "before_count": 4, "after_count": 4, "dropped_count": 0}
        assert body["messages"] is msgs
        assert not any(s["name"] == "history_control" for s in body["_gitv_debug"]["stages"])

    def test_drop_noop_does_not_increment_ops_applied(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
        ]
        # Index 1 is the protected newest user message; 99 is out of bounds.
        body = {"messages": msgs, "_gitv_history_ops": [{"op": "drop", "indices": [1, -1, 99]}]}
        init_debug(body, [])

        summary = apply_history_ops(body)

        assert summary == {"ops_applied": 0, "before_count": 2, "after_count": 2, "dropped_count": 0}
        assert body["messages"] is msgs
        assert not any(s["name"] == "history_control" for s in body["_gitv_debug"]["stages"])

    def test_mixed_noop_and_mutating_op_counts_only_mutating(self):
        msgs = [
            {"role": "system", "content": "sys0"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        body = {
            "messages": copy.deepcopy(msgs),
            "_gitv_history_ops": [
                {"op": "keep_recent", "count": 10},  # no-op
                {"op": "drop", "indices": [99]},  # no-op
                {"op": "drop", "indices": [1]},  # drops u1
            ],
        }
        init_debug(body, [])

        summary = apply_history_ops(body)

        assert summary == {"ops_applied": 1, "before_count": 4, "after_count": 3, "dropped_count": 1}
        assert [m["content"] for m in body["messages"]] == ["sys0", "a1", "u2"]
        stages = [s for s in body["_gitv_debug"]["stages"] if s["name"] == "history_control"]
        assert len(stages) == 1
        assert "1 op(s) applied" in stages[0]["detail"]


async def _create_rule_and_snapshot(client, regex_pattern: str = r"\bsecret\b") -> tuple[str, str]:
    created = await client.post(
        "/api/verification/rules",
        json={
            "name": "Regex Guard",
            "prompt": "Check for secrets",
            "regex_pattern": regex_pattern,
        },
    )
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]

    snap = await client.post(
        "/api/snapshots",
        json={"resource_type": "verification_rule", "resource_id": rule_id, "label": "v1"},
    )
    assert snap.status_code == 201, snap.text
    return rule_id, snap.json()["id"]


async def _tamper_snapshot_regex(snapshot_id: str, new_pattern: str) -> None:
    async with TestSessionLocal() as db:
        result = await db.execute(select(ResourceSnapshot).where(ResourceSnapshot.id == snapshot_id))
        snap = result.scalar_one()
        content = json.loads(snap.content_json)
        content["regex_pattern"] = new_pattern
        snap.content_json = json.dumps(content)
        await db.commit()


class TestVerificationRuleSnapshotRegexValidation:
    async def test_restore_rejects_invalid_regex_pattern(self, admin_client):
        client, _token, _key = admin_client
        _rule_id, snap_id = await _create_rule_and_snapshot(client)
        await _tamper_snapshot_regex(snap_id, "[unclosed")

        resp_new = await client.post(f"/api/snapshots/{snap_id}/restore-as-new", json={})
        assert resp_new.status_code == 422, resp_new.text
        assert "Invalid regex pattern" in resp_new.json()["detail"]

        resp_in_place = await client.post(f"/api/snapshots/{snap_id}/restore-in-place", json={})
        assert resp_in_place.status_code == 422, resp_in_place.text
        assert "Invalid regex pattern" in resp_in_place.json()["detail"]

    async def test_restore_rejects_oversized_regex_pattern(self, admin_client):
        client, _token, _key = admin_client
        _rule_id, snap_id = await _create_rule_and_snapshot(client)
        await _tamper_snapshot_regex(snap_id, "a" * 1025)

        resp_new = await client.post(f"/api/snapshots/{snap_id}/restore-as-new", json={})
        assert resp_new.status_code == 422, resp_new.text
        assert "Regex pattern too long" in resp_new.json()["detail"]

        resp_in_place = await client.post(f"/api/snapshots/{snap_id}/restore-in-place", json={})
        assert resp_in_place.status_code == 422, resp_in_place.text
        assert "Regex pattern too long" in resp_in_place.json()["detail"]

    async def test_restore_allows_valid_regex_pattern(self, admin_client):
        client, _token, _key = admin_client
        rule_id, snap_id = await _create_rule_and_snapshot(client, regex_pattern=r"\bsecret\b")

        resp_new = await client.post(
            f"/api/snapshots/{snap_id}/restore-as-new", json={"name": "Restored Regex Rule"}
        )
        assert resp_new.status_code == 200, resp_new.text
        new_id = resp_new.json()["resource_id"]
        fetched_new = await client.get(f"/api/verification/rules/{new_id}")
        assert fetched_new.status_code == 200
        assert fetched_new.json()["regex_pattern"] == r"\bsecret\b"

        # Mutate the live rule then restore in place
        upd = await client.put(
            f"/api/verification/rules/{rule_id}",
            json={"regex_pattern": r"\bother\b"},
        )
        assert upd.status_code == 200, upd.text

        resp_in_place = await client.post(f"/api/snapshots/{snap_id}/restore-in-place", json={})
        assert resp_in_place.status_code == 200, resp_in_place.text
        fetched_live = await client.get(f"/api/verification/rules/{rule_id}")
        assert fetched_live.status_code == 200
        assert fetched_live.json()["regex_pattern"] == r"\bsecret\b"
