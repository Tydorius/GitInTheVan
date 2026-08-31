"""Map pipeline tests.

Covers the three defects that only surface once a map runs more than two stages:

- stage endpoint_tag must be settable and must survive an export/import round
  trip, otherwise a shared map cannot name the *role* a stage needs and the
  Phase 16 tag resolver in ``_resolve_stage_endpoint`` is unreachable;
- each stage's output must reach later stages exactly once, not once per
  remaining stage;
- one stage's skills, samples and lorebooks must not leak into the next stage's
  request.
"""
import json

import pytest


async def _make_endpoint(client, name, host, tag, priority=1):
    resp = await client.post("/api/endpoints", json={
        "name": name, "base_url": f"http://{host}", "api_key": f"sk-{name.lower()}",
        "role_tag": tag, "priority": priority,
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _skill_resource(name, content, rtype="skill", sticky=False):
    return {
        "resource_type": rtype,
        "position": "pre_driver",
        "sticky": sticky,
        "resource_name": name,
        "resource_content": {
            "name": name, "description": "", "content": content, "type": rtype,
        },
    }


def _stage(order, name, tag, output_mode, resources=None):
    return {
        "stage_order": order,
        "name": name,
        "system_instructions": f"Instructions for {name}.",
        "endpoint_tag": tag,
        "model_override": "",
        "driver_callable_turns": 0,
        "verification_enabled": False,
        "output_mode": output_mode,
        "resources": resources or [],
    }


def _bodies_for(httpx_mock, host):
    """Request bodies POSTed to a given mock host, in order."""
    return [
        json.loads(r.content)
        for r in httpx_mock.get_requests()
        if r.url.host == host
    ]


class TestStageEndpointTag:
    @pytest.mark.asyncio
    async def test_endpoint_tag_survives_export_import(self, admin_client):
        """endpoint_tag is the model-swap lever: a shared map names a role, and
        the importer points that tag at whatever model they run this month."""
        client, _, _ = admin_client

        created = await client.post("/api/maps", json={
            "name": "Tagged", "tag": "",
            "stages": [
                _stage(1, "Plan", "planner", "sanitize"),
                _stage(2, "Write", "drafter", "persist"),
            ],
        })
        assert created.status_code == 201, created.text
        map_id = created.json()["id"]
        assert [s["endpoint_tag"] for s in created.json()["stages"]] == [
            "planner", "drafter"
        ]

        exported = await client.get(f"/api/maps/{map_id}/export")
        assert exported.status_code == 200
        assert [s["endpoint_tag"] for s in exported.json()["stages"]] == [
            "planner", "drafter"
        ]

        reimported = await client.post(
            "/api/maps/import", json={"name": "Tagged Copy", "data": exported.json()}
        )
        assert reimported.status_code == 201, reimported.text
        assert [s["endpoint_tag"] for s in reimported.json()["stages"]] == [
            "planner", "drafter"
        ]

    @pytest.mark.asyncio
    async def test_endpoint_tag_routes_each_stage(self, admin_client, httpx_mock):
        """A tagged stage resolves to its tagged endpoint, not the user default."""
        client, _, api_key = admin_client

        default_id = await _make_endpoint(client, "Default", "default.test", "")
        await _make_endpoint(client, "Planner", "planner.test", "planner")
        await client.put("/api/settings", json={"default_endpoint_id": default_id})

        created = await client.post("/api/maps", json={
            "name": "Routed", "tag": "",
            "stages": [
                _stage(1, "Plan", "planner", "sanitize"),
                _stage(2, "Write", "", "persist"),
            ],
        })
        await client.put("/api/settings", json={"default_map_id": created.json()["id"]})

        httpx_mock.add_response(
            url="http://planner.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "PLAN"}}]},
        )
        httpx_mock.add_response(
            url="http://default.test/v1/chat/completions",
            json={"choices": [{"message": {"content": "SCENE"}}]},
        )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Write a scene"}],
        }, headers={"Authorization": f"Bearer {api_key}"})

        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "SCENE"
        assert len(_bodies_for(httpx_mock, "planner.test")) == 1
        assert len(_bodies_for(httpx_mock, "default.test")) == 1


class TestFourStagePipeline:
    """A Plan -> Draft -> Critique -> Polish map, which is where the accumulation
    and leakage bugs become visible. At two stages both are invisible."""

    @pytest.fixture
    async def four_stage_run(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        await client.put("/api/admin/settings", json={"max_map_stages": 4})

        for name, host, tag in [
            ("Planner", "planner.test", "planner"),
            ("Drafter", "drafter.test", "drafter"),
            ("Critic", "critic.test", "critic"),
            ("Polisher", "polisher.test", "polisher"),
        ]:
            await _make_endpoint(client, name, host, tag)

        # Built through /import so the skills and samples can be embedded, which
        # is how the published example maps ship.
        created = await client.post("/api/maps/import", json={"data": {
            "name": "Overthink",
            "global_llm_instructions": "GLOBAL_RULES",
            "stages": [
                _stage(1, "Architect", "planner", "sanitize",
                       [_skill_resource("PlanSkill", "PLANNING_SKILL_TEXT")]),
                _stage(2, "Prose", "drafter", "sanitize", [
                    _skill_resource("Anchor", "STYLE_ANCHOR_TEXT", "sample"),
                    _skill_resource("ProseSkill", "PROSE_SKILL_TEXT"),
                ]),
                _stage(3, "Line Editor", "critic", "sanitize",
                       [_skill_resource("Checklist", "LLMISM_CHECKLIST_TEXT")]),
                _stage(4, "Final Pass", "polisher", "persist",
                       [_skill_resource("Anchor2", "STYLE_ANCHOR_TEXT", "sample")]),
            ],
        }})
        assert created.status_code == 201, created.text
        assert len(created.json()["stages"]) == 4
        await client.put("/api/settings", json={"default_map_id": created.json()["id"]})

        for host, out in [
            ("planner.test", "THE_PLAN"),
            ("drafter.test", "THE_DRAFT"),
            ("critic.test", "THE_CRITIQUE"),
            ("polisher.test", "THE_FINAL_PROSE"),
        ]:
            httpx_mock.add_response(
                url=f"http://{host}/v1/chat/completions",
                json={"choices": [{"message": {"content": out}}]},
            )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Write a scene"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, resp.text
        return resp, httpx_mock

    @pytest.mark.asyncio
    async def test_all_four_stages_run_in_order(self, four_stage_run):
        _, httpx_mock = four_stage_run
        hosts = [r.url.host for r in httpx_mock.get_requests()]
        assert hosts == [
            "planner.test", "drafter.test", "critic.test", "polisher.test"
        ]

    @pytest.mark.asyncio
    async def test_only_final_stage_output_reaches_the_client(self, four_stage_run):
        resp, _ = four_stage_run
        content = resp.json()["choices"][0]["message"]["content"]
        assert content == "THE_FINAL_PROSE"
        for leaked in ("THE_PLAN", "THE_DRAFT", "THE_CRITIQUE"):
            assert leaked not in content

    @pytest.mark.asyncio
    async def test_each_stage_output_appears_exactly_once_downstream(
        self, four_stage_run
    ):
        """Regression: sticky_context used to be re-appended in full on every
        iteration, so stage 1's output reached stage 4 three times."""
        _, httpx_mock = four_stage_run
        final = json.dumps(_bodies_for(httpx_mock, "polisher.test")[0])

        assert final.count("THE_PLAN") == 1
        assert final.count("THE_DRAFT") == 1
        assert final.count("THE_CRITIQUE") == 1

    @pytest.mark.asyncio
    async def test_sanitize_wraps_prior_output_with_its_stage_name(
        self, four_stage_run
    ):
        _, httpx_mock = four_stage_run
        final = json.dumps(_bodies_for(httpx_mock, "polisher.test")[0])

        assert "[STAGE 1 OUTPUT: Architect]" in final
        assert "[STAGE 2 OUTPUT: Prose]" in final
        assert "[STAGE 3 OUTPUT: Line Editor]" in final

    @pytest.mark.asyncio
    async def test_stage_resources_do_not_leak_into_later_stages(
        self, four_stage_run
    ):
        """Regression: inject_skills appends into messages[0]["content"] and
        inject_samples inserts a free-standing system message, neither of which
        the old [STAGE INSTRUCTIONS] filter could remove."""
        _, httpx_mock = four_stage_run

        plan_body = json.dumps(_bodies_for(httpx_mock, "planner.test")[0])
        draft_body = json.dumps(_bodies_for(httpx_mock, "drafter.test")[0])
        critic_body = json.dumps(_bodies_for(httpx_mock, "critic.test")[0])
        final_body = json.dumps(_bodies_for(httpx_mock, "polisher.test")[0])

        # Each stage sees its own resources.
        assert "PLANNING_SKILL_TEXT" in plan_body
        assert "PROSE_SKILL_TEXT" in draft_body
        assert "STYLE_ANCHOR_TEXT" in draft_body
        assert "LLMISM_CHECKLIST_TEXT" in critic_body
        assert "STYLE_ANCHOR_TEXT" in final_body

        # And nobody else's.
        assert "PLANNING_SKILL_TEXT" not in draft_body
        assert "PLANNING_SKILL_TEXT" not in critic_body
        assert "PLANNING_SKILL_TEXT" not in final_body
        assert "STYLE_ANCHOR_TEXT" not in critic_body
        assert "PROSE_SKILL_TEXT" not in critic_body
        assert "PROSE_SKILL_TEXT" not in final_body
        assert "LLMISM_CHECKLIST_TEXT" not in final_body

        # The style anchor is carried by two separate stages; it must appear once
        # in each of their requests, not twice in the later one.
        assert draft_body.count("STYLE_ANCHOR_TEXT") == 1
        assert final_body.count("STYLE_ANCHOR_TEXT") == 1

    @pytest.mark.asyncio
    async def test_stage_instructions_do_not_accumulate(self, four_stage_run):
        _, httpx_mock = four_stage_run
        final = json.dumps(_bodies_for(httpx_mock, "polisher.test")[0])

        assert final.count("[STAGE INSTRUCTIONS]") == 1
        assert final.count("Instructions for Final Pass.") == 1
        assert "Instructions for Architect." not in final
        assert "Instructions for Prose." not in final
        assert "Instructions for Line Editor." not in final
        # The map-wide instructions ride along with each stage's block, once.
        assert final.count("GLOBAL_RULES") == 1


class TestStickyResources:
    """README "Resource Attachments": a sticky attachment persists through all
    later stages; a stage-only one (the default) is dropped after its own stage.
    Before the snapshot reset neither held -- every attachment leaked forward,
    so sticky was indistinguishable from stage-only."""

    @pytest.fixture
    async def sticky_run(self, admin_client, httpx_mock):
        client, _, api_key = admin_client

        await _make_endpoint(client, "A", "one.test", "one")
        await _make_endpoint(client, "B", "two.test", "two")
        await _make_endpoint(client, "C", "three.test", "three")

        created = await client.post("/api/maps/import", json={"data": {
            "name": "Sticky",
            "stages": [
                _stage(1, "First", "one", "sanitize", [
                    _skill_resource("Carried", "STICKY_TEXT", sticky=True),
                    _skill_resource("Local", "STAGE_ONLY_TEXT"),
                ]),
                _stage(2, "Second", "two", "sanitize", []),
                _stage(3, "Third", "three", "persist", []),
            ],
        }})
        assert created.status_code == 201, created.text
        await client.put("/api/settings", json={"default_map_id": created.json()["id"]})

        for host, out in [
            ("one.test", "ONE"), ("two.test", "TWO"), ("three.test", "THREE")
        ]:
            httpx_mock.add_response(
                url=f"http://{host}/v1/chat/completions",
                json={"choices": [{"message": {"content": out}}]},
            )

        resp = await client.post("/v1/chat/completions", json={
            "model": "test", "messages": [{"role": "user", "content": "Go"}],
        }, headers={"Authorization": f"Bearer {api_key}"})
        assert resp.status_code == 200, resp.text
        return httpx_mock

    @pytest.mark.asyncio
    async def test_sticky_resource_carries_forward_once_per_stage(self, sticky_run):
        for host in ("one.test", "two.test", "three.test"):
            body = json.dumps(_bodies_for(sticky_run, host)[0])
            assert body.count("STICKY_TEXT") == 1, host

    @pytest.mark.asyncio
    async def test_stage_only_resource_does_not_carry_forward(self, sticky_run):
        assert "STAGE_ONLY_TEXT" in json.dumps(_bodies_for(sticky_run, "one.test")[0])
        assert "STAGE_ONLY_TEXT" not in json.dumps(_bodies_for(sticky_run, "two.test")[0])
        assert "STAGE_ONLY_TEXT" not in json.dumps(_bodies_for(sticky_run, "three.test")[0])


class TestMapPackParameterRoundTrip:
    """A shared map carries its tuning with it, and a pack is untrusted input.

    Both halves matter: parameters must survive an export/import cycle, and a
    hand-edited pack must not be able to write a reserved name or an oversized
    blob into the importer's database by going around the API.
    """

    @staticmethod
    def _map_with(params, vparams=None):
        return {
            "name": "Tuned",
            "tag": "",
            "stages": [
                {
                    "name": "Write",
                    "endpoint_tag": "drafter",
                    "output_mode": "persist",
                    "parameters": params,
                    "verification_parameters": vparams or [],
                }
            ],
        }

    @pytest.mark.asyncio
    async def test_parameters_survive_export_and_import(self, admin_client):
        client, _, _ = admin_client
        created = await client.post(
            "/api/maps",
            json=self._map_with(
                [
                    {
                        "name": "reasoning_effort",
                        "type": "string",
                        "value": "low",
                        "description": "Cheap stage",
                        "required": True,
                        "options": ["low", "high"],
                    }
                ],
                [{"name": "max_tokens", "type": "integer", "value": 50}],
            ),
        )
        assert created.status_code == 201, created.text
        map_id = created.json()["id"]

        exported = await client.get(f"/api/maps/{map_id}/export")
        assert exported.status_code == 200
        stage = exported.json()["stages"][0]
        assert stage["parameters"][0]["value"] == "low"
        assert stage["parameters"][0]["options"] == ["low", "high"]
        assert stage["verification_parameters"][0]["value"] == 50

        reimported = await client.post(
            "/api/maps/import", json={"name": "Tuned Copy", "data": exported.json()}
        )
        assert reimported.status_code == 201, reimported.text
        got = reimported.json()["stages"][0]
        assert got["parameters"][0]["name"] == "reasoning_effort"
        assert got["parameters"][0]["value"] == "low"
        assert got["parameters"][0]["required"] is True
        assert got["parameters"][0]["description"] == "Cheap stage"
        assert got["verification_parameters"][0]["value"] == 50

    @pytest.mark.asyncio
    async def test_a_pre_0_24_pack_still_installs(self, admin_client):
        """Packs published before parameters existed carry no such key."""
        client, _, _ = admin_client
        resp = await client.post(
            "/api/maps/import",
            json={
                "data": {
                    "name": "Legacy",
                    "stages": [{"stage_order": 1, "name": "Write", "output_mode": "persist"}],
                }
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["stages"][0]["parameters"] == []
        assert resp.json()["stages"][0]["verification_parameters"] == []

    @pytest.mark.asyncio
    async def test_a_pack_cannot_smuggle_a_reserved_name(self, admin_client):
        """Hand-edited pack JSON goes around the API's validator, so the
        importer runs its own. `stream` is owned by the pipeline."""
        client, _, _ = admin_client
        resp = await client.post(
            "/api/maps/import",
            json={
                "data": {
                    "name": "Hostile",
                    "stages": [
                        {
                            "stage_order": 1,
                            "name": "Write",
                            "output_mode": "persist",
                            "parameters": [
                                {"name": "stream", "type": "boolean", "value": True},
                                {"name": "temperature", "type": "float", "value": 0.5},
                            ],
                        }
                    ],
                }
            },
        )
        # The map still installs -- losing a stage's tuning is recoverable,
        # losing the map is not -- but the parameters are dropped.
        assert resp.status_code == 201, resp.text
        assert resp.json()["stages"][0]["parameters"] == []

    @pytest.mark.asyncio
    async def test_a_pack_cannot_smuggle_an_oversized_blob(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/maps/import",
            json={
                "data": {
                    "name": "Huge",
                    "stages": [
                        {
                            "stage_order": 1,
                            "name": "Write",
                            "output_mode": "persist",
                            "parameters": [{"name": "big", "value": "x" * 20000}],
                        }
                    ],
                }
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["stages"][0]["parameters"] == []

    @pytest.mark.asyncio
    async def test_a_pack_with_malformed_parameters_still_installs(self, admin_client):
        client, _, _ = admin_client
        resp = await client.post(
            "/api/maps/import",
            json={
                "data": {
                    "name": "Broken",
                    "stages": [
                        {
                            "stage_order": 1,
                            "name": "Write",
                            "output_mode": "persist",
                            "parameters": "not a list",
                        }
                    ],
                }
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["stages"][0]["parameters"] == []
