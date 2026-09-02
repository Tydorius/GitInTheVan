"""Self-check tools (sub-phase 26d).

These run against the real app and a real (in-memory) database -- the point of
a self-check is that it tells the truth about the caller's own live
configuration, so mocking the database would test nothing. Only the outer
boundary (upstream LLM calls) is mocked, via `httpx_mock`.
"""

from __future__ import annotations

import json

import pytest

from app.main import app
from app.services.admin import get_admin_settings
from app.services.assistant.context import ToolContext
from app.services.assistant.executor import execute
from app.services.assistant.registry import TOOLS, Risk
from app.services.deno_runner import DENO_PATH

SKIP_DENO = not DENO_PATH

TRIVIAL_CANTRIP = 'context.tool_result = "selfcheck-ok";'


async def _ctx(client, token, *, is_admin=True, secrets=()):
    me = await client.get("/api/auth/me")
    assert me.status_code == 200
    admin = await get_admin_settings()
    return ToolContext(
        user_id=me.json()["id"],
        is_admin=is_admin,
        bearer_token=token,
        app=app,
        conversation_id="conv-selfcheck",
        admin=admin,
        secrets=secrets,
    )


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
    return login.json()["access_token"], created.json()["id"]


def _ok(outcome):
    assert outcome.ok, outcome.result
    return outcome.result


def _find(results, substr):
    return [r for r in results if substr in r["name"]]


class TestRegistration:
    def test_reads_are_read(self):
        for name in (
            "list_self_checks",
            "dry_run_activation",
            "dry_run_lorebook",
            "explain_parameters",
            "lint_configuration",
            "report_routing",
            "run_sandbox_smoke",
        ):
            assert TOOLS[name].group == "selfcheck", name
            assert TOOLS[name].risk is Risk.READ, name
            assert TOOLS[name].kind == "local", name

    def test_probes_are_external_cost(self):
        for name in ("probe_endpoint", "probe_judge", "probe_summarizer", "probe_map"):
            assert TOOLS[name].group == "selfcheck", name
            assert TOOLS[name].risk is Risk.EXTERNAL_COST, name
            assert TOOLS[name].kind == "local", name

    @pytest.mark.asyncio
    async def test_list_self_checks_names_every_other_selfcheck_tool(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = await execute(ctx, TOOLS["list_self_checks"], {})
        result = _ok(outcome)
        listed = {c["name"] for c in result["checks"]}
        expected = {
            t.name
            for t in TOOLS.values()
            if t.group == "selfcheck" and t.name != "list_self_checks"
        }
        assert listed == expected


class TestDryRunActivation:
    @pytest.mark.asyncio
    async def test_would_activate_when_tag_present_not_when_absent(self, admin_client):
        client, token, _ = admin_client
        created = await client.post(
            "/api/cantrips",
            json={
                "name": "Healer",
                "code": TRIVIAL_CANTRIP,
                "tag": "heal",
                "is_active": False,
            },
        )
        assert created.status_code == 201

        ctx = await _ctx(client, token)

        with_tag = _ok(
            await execute(ctx, TOOLS["dry_run_activation"], {"message": "<#cantrip-heal#> please help"})
        )
        healer = _find(with_tag, "cantrip: Healer")
        assert healer and healer[0]["passed"] is True

        without_tag = _ok(
            await execute(ctx, TOOLS["dry_run_activation"], {"message": "just chatting"})
        )
        healer2 = _find(without_tag, "cantrip: Healer")
        assert healer2 and healer2[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_second_users_public_cantrip_activates_by_tag(self, admin_client):
        client, token, _ = admin_client
        other_token, _ = await _second_user(client)

        made = await client.post(
            "/api/cantrips",
            json={
                "name": "Shared",
                "code": TRIVIAL_CANTRIP,
                "tag": "share",
                "is_public": True,
                "is_active": False,
            },
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert made.status_code == 201

        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["dry_run_activation"], {"message": "<#cantrip-share#> hello"})
        )
        shared = _find(outcome, "cantrip: Shared")
        assert shared and shared[0]["passed"] is True

        # And a *private* second-user cantrip, tagged the same way, must not
        # even be evaluated for someone else -- it is invisible to this
        # caller's query entirely, not merely reported as "would not fire".
        made_private = await client.post(
            "/api/cantrips",
            json={"name": "Theirs", "code": TRIVIAL_CANTRIP, "tag": "private-thing", "is_public": False},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        assert made_private.status_code == 201
        outcome2 = _ok(
            await execute(
                ctx, TOOLS["dry_run_activation"], {"message": "<#cantrip-private-thing#> hello"}
            )
        )
        theirs = _find(outcome2, "cantrip: Theirs")
        assert theirs == []

    @pytest.mark.asyncio
    async def test_explicit_tags_argument_is_honoured(self, admin_client):
        client, token, _ = admin_client
        await client.post(
            "/api/cantrips",
            json={"name": "ViaArg", "code": TRIVIAL_CANTRIP, "tag": "arg-tag", "is_active": False},
        )
        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(
                ctx,
                TOOLS["dry_run_activation"],
                {"message": "no tag syntax here", "tags": ["cantrip-arg-tag"]},
            )
        )
        via_arg = _find(outcome, "cantrip: ViaArg")
        assert via_arg and via_arg[0]["passed"] is True

    @pytest.mark.asyncio
    async def test_reports_command_tags_and_parsed_tags(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["dry_run_activation"], {"message": "<VERIFY:off> hello"})
        )
        commands = _find(outcome, "command tags")
        assert commands and "1 command tag" in commands[0]["message"]

    @pytest.mark.asyncio
    async def test_skill_reports_attachment_not_a_tag(self, admin_client):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "E1", "base_url": "https://e1.test", "api_key": "sk-e1"}
        )
        endpoint_id = ep.json()["id"]
        skill = await client.post("/api/skills", json={"name": "MySkill", "content": "Be terse."})
        skill_id = skill.json()["id"]

        ctx = await _ctx(client, token)
        before = _ok(await execute(ctx, TOOLS["dry_run_activation"], {"message": "hi"}))
        skill_result = _find(before, "skill: MySkill")
        assert skill_result and skill_result[0]["passed"] is False

        attach = await client.post(f"/api/skills/{skill_id}/attach", json={"endpoint_id": endpoint_id})
        assert attach.status_code in (200, 201)

        after = _ok(await execute(ctx, TOOLS["dry_run_activation"], {"message": "hi"}))
        skill_result2 = _find(after, "skill: MySkill")
        assert skill_result2 and skill_result2[0]["passed"] is True


class TestDryRunLorebook:
    @pytest.mark.asyncio
    async def test_fires_keyword_entry_and_skips_disabled(self, admin_client):
        client, token, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "Bestiary"})
        lorebook_id = lb.json()["id"]

        fires = await client.post(
            f"/api/lorebooks/{lorebook_id}/entries",
            json={"name": "Dragon", "keys": ["dragon"], "content": "A dragon guards the pass."},
        )
        assert fires.status_code == 201

        disabled = await client.post(
            f"/api/lorebooks/{lorebook_id}/entries",
            json={
                "name": "Ghost",
                "keys": ["ghost"],
                "content": "A ghost haunts the tower.",
                "is_disabled": True,
            },
        )
        assert disabled.status_code == 201

        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(
                ctx,
                TOOLS["dry_run_lorebook"],
                {"text": "I see a dragon and a ghost in the distance.", "lorebook_id": lorebook_id},
            )
        )

        fired = _find(outcome, "fired: Dragon")
        assert fired and fired[0]["passed"] is True

        skipped = _find(outcome, "skipped: Ghost")
        assert skipped and skipped[0]["passed"] is False
        assert "disabled" in skipped[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_no_key_match_is_reported(self, admin_client):
        client, token, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "Empty match"})
        lorebook_id = lb.json()["id"]
        await client.post(
            f"/api/lorebooks/{lorebook_id}/entries",
            json={"name": "Unrelated", "keys": ["griffin"], "content": "A griffin soars above."},
        )

        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(
                ctx, TOOLS["dry_run_lorebook"], {"text": "Nothing relevant here.", "lorebook_id": lorebook_id}
            )
        )
        skipped = _find(outcome, "skipped: Unrelated")
        assert skipped and "no key matched" in skipped[0]["message"].lower()

    @pytest.mark.asyncio
    async def test_unknown_lorebook_id_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["dry_run_lorebook"], {"text": "hi", "lorebook_id": "nope"})
        )
        assert len(outcome) == 1
        assert outcome[0]["passed"] is False


class TestExplainParameters:
    @pytest.mark.asyncio
    async def test_unknown_scope_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["explain_parameters"], {"scope": "nonsense"})
        )
        assert outcome[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_endpoint_scope_reports_the_winning_layer(self, admin_client):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints",
            json={
                "name": "Params",
                "base_url": "https://params.test",
                "api_key": "sk-params",
                "default_model": "gpt-4",
            },
        )
        endpoint_id = ep.json()["id"]
        upd = await client.put(
            f"/api/endpoints/{endpoint_id}",
            json={"parameters": [{"name": "temperature", "type": "number", "value": 0.5}]},
        )
        assert upd.status_code == 200

        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["explain_parameters"], {"scope": "endpoint", "id": endpoint_id})
        )
        assert len(outcome) == 1
        detail = json.loads(outcome[0]["detail"])
        assert detail["values"]["temperature"] == 0.5
        assert "endpoint" in detail["sources"]["temperature"]


class TestLintConfiguration:
    @pytest.mark.asyncio
    async def test_flags_invalid_forbidden_word_regex(self, admin_client):
        client, token, _ = admin_client
        created = await client.post(
            "/api/forbidden-words", json={"phrase": "[unterminated(", "is_regex": True}
        )
        assert created.status_code == 201

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        bad = _find(outcome, "forbidden word regex")
        assert bad and bad[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_flags_map_stage_with_unmatched_endpoint_tag(self, admin_client):
        client, token, _ = admin_client
        created = await client.post(
            "/api/maps",
            json={
                "name": "Broken Map",
                "stages": [{"name": "Stage 1", "endpoint_tag": "no-such-tag"}],
            },
        )
        assert created.status_code == 201

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        bad = _find(outcome, "map stage endpoint tag")
        assert bad and bad[0]["passed"] is False
        assert "Broken Map" in bad[0]["name"]

    @pytest.mark.asyncio
    async def test_clean_configuration_reports_passing_summaries(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        assert outcome, "lint_configuration must return at least the passing summaries"
        assert all(r["passed"] for r in outcome), outcome
        names = {r["name"] for r in outcome}
        assert "parameter blobs" in names
        assert "forbidden word regexes" in names
        assert "endpoint references" in names
        assert "map stage endpoint tags" in names
        assert "tag group members" in names
        assert "safety re-scan" in names
        assert "sanitization sweep" in names

    @pytest.mark.asyncio
    async def test_flags_disabled_endpoint_reference(self, admin_client):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "Dead", "base_url": "https://dead.test", "api_key": "sk-dead"}
        )
        endpoint_id = ep.json()["id"]
        await client.put(f"/api/endpoints/{endpoint_id}", json={"enabled": False})
        await client.put("/api/verification/settings", json={"verification_endpoint_id": endpoint_id})

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        bad = _find(outcome, "endpoint reference")
        assert bad and any(not r["passed"] for r in bad)

    @pytest.mark.asyncio
    async def test_flags_missing_tag_group_member(self, admin_client):
        client, token, _ = admin_client
        lb = await client.post("/api/lorebooks", json={"name": "ToDelete"})
        lorebook_id = lb.json()["id"]
        group = await client.post("/api/tag-groups", json={"name": "G1", "tag": "g1"})
        group_id = group.json()["id"]
        members = await client.put(
            f"/api/tag-groups/{group_id}/members",
            json={"members": [{"member_type": "lorebook", "member_id": lorebook_id}]},
        )
        assert members.status_code == 200
        deleted = await client.delete(f"/api/lorebooks/{lorebook_id}")
        assert deleted.status_code == 204

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["lint_configuration"], {}))
        bad = _find(outcome, "tag group member")
        assert bad and any(not r["passed"] for r in bad)


class TestReportRouting:
    @pytest.mark.asyncio
    async def test_never_contains_the_seeded_api_key(self, admin_client):
        client, token, _ = admin_client
        secret = "sk-ROUTING-SECRET-VALUE"
        created = await client.post(
            "/api/endpoints", json={"name": "Route", "base_url": "https://route.test", "api_key": secret}
        )
        assert created.status_code == 201

        ctx = await _ctx(client, token, secrets=(secret,))
        outcome = _ok(await execute(ctx, TOOLS["report_routing"], {}))
        blob = json.dumps(outcome)
        assert secret not in blob

        chains = _find(outcome, "failover chain")
        assert chains and chains[0]["passed"] is True
        detail = json.loads(chains[0]["detail"])
        assert detail[0]["api_key_set"] is True
        assert "api_key" not in detail[0]

    @pytest.mark.asyncio
    async def test_reports_api_key_label_routing(self, admin_client):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "Labeled", "base_url": "https://labeled.test", "api_key": "sk-l"}
        )
        endpoint_id = ep.json()["id"]
        created = await client.post(
            "/api/api-keys", json={"label": "mobile", "endpoint_id": endpoint_id}
        )
        assert created.status_code in (200, 201), created.text

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["report_routing"], {}))
        keys = _find(outcome, "api key: mobile")
        assert keys and keys[0]["passed"] is True
        assert "Labeled" in keys[0]["message"]


@pytest.mark.skipif(SKIP_DENO, reason="Deno not available")
class TestRunSandboxSmoke:
    @pytest.mark.asyncio
    async def test_smoke_test_and_active_cantrip_run(self, admin_client):
        client, token, _ = admin_client
        created = await client.post(
            "/api/cantrips", json={"name": "Active", "code": TRIVIAL_CANTRIP, "is_active": True}
        )
        assert created.status_code == 201

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["run_sandbox_smoke"], {}))
        smoke = _find(outcome, "sandbox smoke test")
        assert smoke and smoke[0]["passed"] is True
        cantrip_result = _find(outcome, "cantrip: Active")
        assert cantrip_result and cantrip_result[0]["passed"] is True


@pytest.mark.asyncio
async def test_run_sandbox_smoke_skips_gracefully_without_deno(monkeypatch):
    """A pure unit check that the absence path never raises, independent of
    whether Deno happens to be installed on the machine running the suite."""
    from app.services.assistant import selfcheck

    class _Ctx:
        user_id = "does-not-matter"
        conversation_id = "c"

    monkeypatch.setattr("app.services.deno_runner._find_deno", lambda: None)
    results = await selfcheck.HANDLERS["run_sandbox_smoke"](_Ctx(), {})
    assert len(results) == 1
    assert results[0].passed is True
    assert "deno" in results[0].message.lower()


class TestProbeEndpoint:
    @pytest.mark.asyncio
    async def test_non_streaming_probe(self, admin_client, httpx_mock):
        client, token, _ = admin_client
        created = await client.post(
            "/api/endpoints",
            json={
                "name": "Probe",
                "base_url": "https://probe.test",
                "api_key": "sk-probe",
                "default_model": "test-model",
            },
        )
        endpoint_id = created.json()["id"]

        httpx_mock.add_response(
            url="https://probe.test/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://probe.test/v1/models",
            json={"data": [{"id": "test-model"}]},
            status_code=200,
        )

        ctx = await _ctx(client, token)
        outcome = _ok(
            await execute(ctx, TOOLS["probe_endpoint"], {"endpoint_id": endpoint_id, "streaming": False})
        )
        probe = _find(outcome, "1-token probe")
        assert probe and probe[0]["passed"] is True
        model_list = _find(outcome, "model list comparison")
        assert model_list

    @pytest.mark.asyncio
    async def test_missing_endpoint_id_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_endpoint"], {"endpoint_id": ""}))
        assert outcome[0]["passed"] is False


class TestProbeJudge:
    @pytest.mark.asyncio
    async def test_violating_and_clean_probes_pass(self, admin_client, httpx_mock):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "Judge", "base_url": "https://judge.test", "api_key": "sk-judge"}
        )
        endpoint_id = ep.json()["id"]
        await client.put(
            "/api/verification/settings",
            json={"verification_endpoint_id": endpoint_id, "verification_model": "judge-model"},
        )

        httpx_mock.add_response(
            url="https://judge.test/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"violation": True, "reason": "Contains BANANA", "severity": "high"}
                            ),
                        }
                    }
                ]
            },
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://judge.test/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"violation": False, "reason": "", "severity": "none"}),
                        }
                    }
                ]
            },
            status_code=200,
        )

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_judge"], {}))
        violating = _find(outcome, "violating probe")
        clean = _find(outcome, "clean probe")
        assert violating and violating[0]["passed"] is True
        assert clean and clean[0]["passed"] is True

    @pytest.mark.asyncio
    async def test_fails_when_judge_says_clean_to_a_violation(self, admin_client, httpx_mock):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "BlindJudge", "base_url": "https://blind.test", "api_key": "sk-blind"}
        )
        endpoint_id = ep.json()["id"]
        await client.put(
            "/api/verification/settings",
            json={"verification_endpoint_id": endpoint_id, "verification_model": "blind-model"},
        )

        httpx_mock.add_response(
            url="https://blind.test/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"violation": False, "reason": "", "severity": "none"}),
                        }
                    }
                ]
            },
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://blind.test/v1/chat/completions",
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"violation": False, "reason": "", "severity": "none"}),
                        }
                    }
                ]
            },
            status_code=200,
        )

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_judge"], {}))
        violating = _find(outcome, "violating probe")
        assert violating and violating[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_no_verification_endpoint_configured_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_judge"], {}))
        assert len(outcome) == 1
        assert outcome[0]["passed"] is False


class TestProbeSummarizer:
    @pytest.mark.asyncio
    async def test_no_summarization_endpoint_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_summarizer"], {}))
        assert len(outcome) == 1
        assert outcome[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_summary_returned(self, admin_client, httpx_mock):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints", json={"name": "Sum", "base_url": "https://sum.test", "api_key": "sk-sum"}
        )
        endpoint_id = ep.json()["id"]
        upd = await client.put(
            "/api/summarization/settings",
            json={"summarization_enabled": True, "summarization_endpoint_id": endpoint_id, "summarization_model": "sum-model"},
        )
        assert upd.status_code == 200

        httpx_mock.add_response(
            url="https://sum.test/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": "A tavern scene summary."}}]},
            status_code=200,
        )

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_summarizer"], {}))
        assert outcome[0]["passed"] is True
        assert "tavern" in outcome[0]["detail"].lower()


class TestProbeMap:
    @pytest.mark.asyncio
    async def test_missing_map_id_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_map"], {"map_id": ""}))
        assert outcome[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_unknown_map_id_is_a_failed_result(self, admin_client):
        client, token, _ = admin_client
        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_map"], {"map_id": "nope"}))
        assert outcome[0]["passed"] is False

    @pytest.mark.asyncio
    async def test_stage_resolution_and_pipeline_run(self, admin_client, httpx_mock):
        client, token, _ = admin_client
        ep = await client.post(
            "/api/endpoints",
            json={"name": "MapEp", "base_url": "https://mapep.test", "api_key": "sk-map", "default_model": "map-model"},
        )
        endpoint_id = ep.json()["id"]
        created = await client.post(
            "/api/maps",
            json={
                "name": "OneStage",
                "stages": [{"name": "Stage 1", "endpoint_id": endpoint_id}],
            },
        )
        assert created.status_code == 201
        map_id = created.json()["id"]

        httpx_mock.add_response(
            url="https://mapep.test/v1/chat/completions",
            json={"choices": [{"message": {"role": "assistant", "content": "Stage output."}}]},
            status_code=200,
        )

        ctx = await _ctx(client, token)
        outcome = _ok(await execute(ctx, TOOLS["probe_map"], {"map_id": map_id}))
        stage = _find(outcome, "stage resolution: Stage 1")
        assert stage and stage[0]["passed"] is True
        run = _find(outcome, "pipeline run")
        assert run and run[0]["passed"] is True
