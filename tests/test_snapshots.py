"""Per-object snapshots and restore (Phase 25).

The tests that matter most here are the ones that would still pass if the
feature were subtly lossy: `test_every_content_column_survives_a_round_trip`
(the reason the serializer reads `__table__.columns` instead of a field list),
and `test_a_conflicting_update_leaves_no_snapshot` (the reason a `pre_edit`
capture can sit before validation at all).
"""

import pytest

from app.models.resource_snapshot import ResourceSnapshot
from app.services.admin import update_admin_settings
from app.services.snapshots import SNAPSHOT_TYPES, content_columns

VALID_CANTRIP = "export default function(context) { return context }"


async def _second_user(client):
    created = await client.post(
        "/api/users",
        json={"username": "other", "password": "otherpass123", "is_admin": False},
    )
    assert created.status_code in (200, 201), created.text
    login = await client.post(
        "/api/auth/login", json={"username": "other", "password": "otherpass123"}
    )
    assert login.status_code == 200
    return login.json()["access_token"]


async def _make_cantrip(client, **overrides):
    payload = {"name": "Dice Controller", "code": VALID_CANTRIP}
    payload.update(overrides)
    resp = await client.post("/api/cantrips", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _snapshots_for(client, resource_type, resource_id):
    resp = await client.get(
        "/api/snapshots",
        params={"resource_type": resource_type, "resource_id": resource_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["snapshots"]


class TestAutomaticCapture:
    async def test_an_update_snapshots_the_previous_version(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)

        assert await _snapshots_for(client, "cantrip", cid) == []

        resp = await client.put(f"/api/cantrips/{cid}", json={"code": "// rewritten"})
        assert resp.status_code == 200

        rows = await _snapshots_for(client, "cantrip", cid)
        assert len(rows) == 1
        assert rows[0]["source"] == "pre_edit"

        detail = await client.get(f"/api/snapshots/{rows[0]['id']}")
        assert detail.json()["content"]["code"] == VALID_CANTRIP

    async def test_a_delete_snapshots_what_it_removes(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client, name="Doomed")

        assert (await client.delete(f"/api/cantrips/{cid}")).status_code == 204

        rows = await _snapshots_for(client, "cantrip", cid)
        assert len(rows) == 1
        assert rows[0]["resource_name"] == "Doomed"

    async def test_repeated_no_op_saves_store_one_row(self, admin_client):
        """Three identical saves are one version, not three."""
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)

        for _ in range(3):
            resp = await client.put(f"/api/cantrips/{cid}", json={"code": VALID_CANTRIP})
            assert resp.status_code == 200

        assert len(await _snapshots_for(client, "cantrip", cid)) == 1

    async def test_a_conflicting_update_leaves_no_snapshot(self, admin_client):
        """The capture sits before the 409 tag check, so it must roll back with it.

        `update_lorebook` interleaves assignment with its uniqueness check, so
        there is no point in these handlers that is after all validation and
        before all mutation. The session rollback is what makes capturing early
        safe, and this is the test that proves it.
        """
        client, _token, _key = admin_client
        first = await _make_cantrip(client, name="First", tag="shared")
        second = await _make_cantrip(client, name="Second")

        resp = await client.put(f"/api/cantrips/{second}", json={"tag": "shared"})
        assert resp.status_code == 409

        assert await _snapshots_for(client, "cantrip", second) == []
        assert await _snapshots_for(client, "cantrip", first) == []


class TestLosslessness:
    @pytest.mark.parametrize("resource_type", sorted(SNAPSHOT_TYPES))
    def test_the_serializer_covers_every_content_column(self, resource_type):
        """Only identity and bookkeeping may be left out.

        This is what stops a column added in a later phase from being silently
        unrestorable: it is captured by introspection, or this fails.
        """
        spec = SNAPSHOT_TYPES[resource_type]
        model_columns = {c.key for c in spec.model.__table__.columns}
        missed = model_columns - set(content_columns(spec))
        assert missed <= {"id", "user_id", "created_at", "updated_at"}
        assert {"id", "user_id"} <= missed

    async def test_every_content_column_survives_a_round_trip(self, admin_client):
        """Not just the fields an export would carry.

        `_content_of_row` in map_transfer.py drops hook_type, the run_* flags,
        is_active, is_public and budget_weight, because a published cantrip does
        not carry a local install's wiring. Restoring through that shape would
        produce an object that looks right and behaves differently.
        """
        client, _token, _key = admin_client
        cid = await _make_cantrip(
            client,
            name="Fully Wired",
            description="every field set",
            llm_instructions="call me",
            hook_type="post",
            run_pre_driver=False,
            run_driver_callable=True,
            run_pre_navigator=True,
            run_post_navigator=True,
            is_public=True,
            is_active=False,
            execution_order=77,
            timeout_ms=9000,
        )
        original = (await client.get(f"/api/cantrips/{cid}")).json()

        assert (
            await client.put(
                f"/api/cantrips/{cid}",
                json={
                    "code": "// flattened",
                    "hook_type": "pre",
                    "run_pre_driver": True,
                    "run_driver_callable": False,
                    "run_pre_navigator": False,
                    "run_post_navigator": False,
                    "is_public": False,
                    "is_active": True,
                    "execution_order": 10,
                    "timeout_ms": 5000,
                    "description": "",
                    "llm_instructions": "",
                },
            )
        ).status_code == 200

        rows = await _snapshots_for(client, "cantrip", cid)
        restore = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
        assert restore.status_code == 200, restore.text

        restored = (await client.get(f"/api/cantrips/{cid}")).json()
        for column in content_columns(SNAPSHOT_TYPES["cantrip"]):
            assert restored[column] == original[column], column


class TestRestoreAsNew:
    async def test_it_copies_without_touching_the_original(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client, name="Original", tag="dice")
        assert (
            await client.put(f"/api/cantrips/{cid}", json={"code": "// broken"})
        ).status_code == 200

        rows = await _snapshots_for(client, "cantrip", cid)
        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-as-new", json={})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] is True
        assert body["resource_id"] != cid

        copy = (await client.get(f"/api/cantrips/{body['resource_id']}")).json()
        assert copy["code"] == VALID_CANTRIP
        assert copy["name"] == "Original (restored)"
        # A tag activates exactly one resource, so the copy must not carry it.
        assert copy["tag"] == ""
        assert any("tag" in note for note in body["notes"])

        live = (await client.get(f"/api/cantrips/{cid}")).json()
        assert live["code"] == "// broken"
        assert live["tag"] == "dice"

    async def test_a_deleted_object_can_be_restored_as_a_copy(self, admin_client):
        client, _token, _key = admin_client
        lid = (
            await client.post("/api/lorebooks", json={"name": "Gone", "description": "d"})
        ).json()["id"]
        entry = await client.post(
            f"/api/lorebooks/{lid}/entries",
            json={"name": "Elf", "keys": ["elf"], "content": "Tall and rude."},
        )
        assert entry.status_code == 201, entry.text

        assert (await client.delete(f"/api/lorebooks/{lid}")).status_code == 204

        rows = await _snapshots_for(client, "lorebook", lid)
        assert len(rows) == 1
        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-as-new", json={})
        assert resp.status_code == 200, resp.text

        restored = await client.get(f"/api/lorebooks/{resp.json()['resource_id']}")
        assert restored.status_code == 200
        entries = restored.json()["entries"]
        assert [e["name"] for e in entries] == ["Elf"]
        assert entries[0]["content"] == "Tall and rude."
        assert entries[0]["keys"] == ["elf"]

    async def test_a_custom_name_is_used(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        assert (await client.delete(f"/api/cantrips/{cid}")).status_code == 204
        rows = await _snapshots_for(client, "cantrip", cid)

        resp = await client.post(
            f"/api/snapshots/{rows[0]['id']}/restore-as-new", json={"name": "Second Try"}
        )
        assert resp.json()["name"] == "Second Try"


class TestRestoreInPlace:
    async def test_it_snapshots_what_it_overwrites(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        assert (
            await client.put(f"/api/cantrips/{cid}", json={"code": "// v2"})
        ).status_code == 200

        rows = await _snapshots_for(client, "cantrip", cid)
        assert len(rows) == 1

        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
        assert resp.status_code == 200, resp.text
        assert resp.json()["created"] is False

        after = await _snapshots_for(client, "cantrip", cid)
        assert len(after) == 2
        # The restore is itself undoable: the version it replaced is now stored.
        newest = await client.get(f"/api/snapshots/{after[0]['id']}")
        assert newest.json()["content"]["code"] == "// v2"
        assert (await client.get(f"/api/cantrips/{cid}")).json()["code"] == VALID_CANTRIP

    async def test_a_deleted_object_is_refused_with_a_reason(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        assert (await client.delete(f"/api/cantrips/{cid}")).status_code == 204
        rows = await _snapshots_for(client, "cantrip", cid)

        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
        assert resp.status_code == 409
        assert "new copy" in resp.json()["detail"]

    async def test_a_taken_tag_is_reported_not_swallowed(self, admin_client):
        """A degradation that keeps working still has to say what it did."""
        client, _token, _key = admin_client
        cid = await _make_cantrip(client, name="Tagged", tag="dice")
        assert (
            await client.put(f"/api/cantrips/{cid}", json={"tag": "other"})
        ).status_code == 200

        # A different cantrip now holds the snapshot's tag.
        await _make_cantrip(client, name="Squatter", tag="dice")

        rows = await _snapshots_for(client, "cantrip", cid)
        oldest = rows[-1]
        resp = await client.post(f"/api/snapshots/{oldest['id']}/restore-in-place")
        assert resp.status_code == 200, resp.text
        notes = resp.json()["notes"]
        assert any("dice" in note for note in notes)

        live = (await client.get(f"/api/cantrips/{cid}")).json()
        assert live["tag"] == "other"

    async def test_a_lorebook_restore_rebuilds_its_entries(self, admin_client):
        client, _token, _key = admin_client
        lid = (await client.post("/api/lorebooks", json={"name": "Bestiary"})).json()["id"]
        for name, content in (("Elf", "Tall."), ("Orc", "Loud.")):
            created = await client.post(
                f"/api/lorebooks/{lid}/entries", json={"name": name, "content": content}
            )
            assert created.status_code == 201, created.text
            if name == "Orc":
                orc_id = created.json()["id"]

        # Deleting an entry snapshots the lorebook as it was, entries and all.
        assert (await client.delete(f"/api/lorebooks/{lid}/entries/{orc_id}")).status_code == 204
        assert [e["name"] for e in (await client.get(f"/api/lorebooks/{lid}")).json()["entries"]] == ["Elf"]

        rows = await _snapshots_for(client, "lorebook", lid)
        assert len(rows) == 1

        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
        assert resp.status_code == 200, resp.text

        restored = (await client.get(f"/api/lorebooks/{lid}")).json()
        assert sorted(e["name"] for e in restored["entries"]) == ["Elf", "Orc"]
        assert {e["name"]: e["content"] for e in restored["entries"]}["Orc"] == "Loud."


class TestManualSnapshots:
    async def test_a_manual_save_is_pinned_and_labelled(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)

        resp = await client.post(
            "/api/snapshots",
            json={"resource_type": "cantrip", "resource_id": cid, "label": "known good"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["source"] == "manual"
        assert resp.json()["label"] == "known good"

    async def test_saving_an_unchanged_object_twice_says_so(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        body = {"resource_type": "cantrip", "resource_id": cid}

        assert (await client.post("/api/snapshots", json=body)).status_code == 201
        second = await client.post("/api/snapshots", json=body)
        assert second.status_code == 409
        assert "identical" in second.json()["detail"]

    async def test_a_missing_object_is_a_404_not_a_silent_no_op(self, admin_client):
        client, _token, _key = admin_client
        resp = await client.post(
            "/api/snapshots",
            json={"resource_type": "cantrip", "resource_id": "no-such-id"},
        )
        assert resp.status_code == 404

    async def test_an_unsupported_type_is_refused(self, admin_client):
        client, _token, _key = admin_client
        resp = await client.post(
            "/api/snapshots", json={"resource_type": "map", "resource_id": "x"}
        )
        assert resp.status_code == 400
        assert "map" in resp.json()["detail"]


class TestPruning:
    async def test_the_cap_trims_automatic_history_but_never_a_pinned_version(
        self, admin_client
    ):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)

        pinned = await client.post(
            "/api/snapshots",
            json={"resource_type": "cantrip", "resource_id": cid, "label": "keep me"},
        )
        assert pinned.status_code == 201
        pinned_id = pinned.json()["id"]

        await update_admin_settings({"max_snapshots_per_object": 3})
        try:
            for i in range(6):
                resp = await client.put(f"/api/cantrips/{cid}", json={"code": f"// v{i}"})
                assert resp.status_code == 200
        finally:
            await update_admin_settings({"max_snapshots_per_object": 20})

        rows = await _snapshots_for(client, "cantrip", cid)
        assert len(rows) <= 4, [r["source"] for r in rows]
        assert pinned_id in {r["id"] for r in rows}


class TestOwnership:
    async def test_another_users_snapshot_is_invisible(self, admin_client):
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        assert (
            await client.put(f"/api/cantrips/{cid}", json={"code": "// v2"})
        ).status_code == 200
        rows = await _snapshots_for(client, "cantrip", cid)
        snapshot_id = rows[0]["id"]

        other = await _second_user(client)
        headers = {"Authorization": f"Bearer {other}"}

        assert (
            await client.get("/api/snapshots", headers=headers)
        ).json()["snapshots"] == []
        assert (
            await client.get(f"/api/snapshots/{snapshot_id}", headers=headers)
        ).status_code == 404
        assert (
            await client.post(
                f"/api/snapshots/{snapshot_id}/restore-in-place", headers=headers
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/snapshots/{snapshot_id}", headers=headers)
        ).status_code == 404


class TestGuards:
    async def test_restore_is_size_checked_like_any_other_content_write(
        self, admin_client
    ):
        """A stored snapshot is not trusted input just because we wrote it.

        The admin's limit may have come down since it was captured.
        """
        client, _token, _key = admin_client
        cid = await _make_cantrip(client, code="x" * 4000)
        assert (
            await client.put(f"/api/cantrips/{cid}", json={"code": "// small"})
        ).status_code == 200
        rows = await _snapshots_for(client, "cantrip", cid)

        await update_admin_settings({"max_script_size_kb": 1})
        try:
            resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_script_size_kb": 50})

        assert (await client.get(f"/api/cantrips/{cid}")).json()["code"] == "// small"


class TestOtherResourceTypes:
    @pytest.mark.parametrize(
        "path,create,update,field",
        [
            (
                "/api/verification/rules",
                {"name": "No banana", "prompt": "must not say banana"},
                {"prompt": "changed"},
                "prompt",
            ),
            (
                "/api/memory-rules",
                {"name": "Trim", "prompt": "summarize hard"},
                {"prompt": "changed"},
                "prompt",
            ),
            (
                "/api/scenario-rules",
                {"name": "Scene", "prompt": "describe the scene"},
                {"prompt": "changed"},
                "prompt",
            ),
            (
                "/api/skills",
                {"name": "Voice", "content": "speak plainly", "type": "skill"},
                {"content": "changed"},
                "content",
            ),
        ],
    )
    async def test_update_then_restore_in_place(
        self, admin_client, path, create, update, field
    ):
        client, _token, _key = admin_client
        created = await client.post(path, json=create)
        assert created.status_code in (200, 201), created.text
        rid = created.json()["id"]

        assert (await client.put(f"{path}/{rid}", json=update)).status_code == 200
        assert (await client.get(f"{path}/{rid}")).json()[field] == update[field]

        listed = await client.get("/api/snapshots", params={"resource_id": rid})
        rows = listed.json()["snapshots"]
        assert len(rows) == 1, rows

        resp = await client.post(f"/api/snapshots/{rows[0]['id']}/restore-in-place")
        assert resp.status_code == 200, resp.text
        assert (await client.get(f"{path}/{rid}")).json()[field] == create[field]


class TestSchemaShape:
    async def test_a_snapshot_outlives_the_row_it_describes(self, admin_client):
        """resource_id carries no foreign key, deliberately.

        A cascade from the resource would delete exactly the snapshot a user
        needs after a mistaken delete.
        """
        client, _token, _key = admin_client
        cid = await _make_cantrip(client)
        assert (await client.delete(f"/api/cantrips/{cid}")).status_code == 204

        assert len(await _snapshots_for(client, "cantrip", cid)) == 1

    def test_resource_id_has_no_foreign_key(self):
        column = ResourceSnapshot.__table__.c.resource_id
        assert column.foreign_keys == set()
