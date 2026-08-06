import pytest


@pytest.fixture
async def auth_client(client):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert setup_resp.status_code == 201
    token = setup_resp.json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    client.headers.pop("Authorization", None)


@pytest.mark.asyncio
class TestSkillCRUD:
    async def test_create_skill(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Combat Expert",
            "description": "Combat writing skill",
            "content": "You are an expert at writing combat scenes.",
            "type": "skill",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Combat Expert"
        assert data["type"] == "skill"
        assert data["content"] == "You are an expert at writing combat scenes."
        assert data["endpoints"] == []

    async def test_create_sample(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Prose Style",
            "content": "Match this writing style...",
            "type": "sample",
        })
        assert resp.status_code == 201
        assert resp.json()["type"] == "sample"

    async def test_invalid_type_rejected(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Bad",
            "type": "invalid",
        })
        assert resp.status_code == 400

    async def test_list_skills(self, auth_client):
        await auth_client.post("/api/skills", json={"name": "Skill 1", "type": "skill"})
        await auth_client.post("/api/skills", json={"name": "Sample 1", "type": "sample"})
        resp = await auth_client.get("/api/skills")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["skills"]) >= 2

    async def test_get_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "Test Skill", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Test Skill"

    async def test_update_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "Original", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.put(f"/api/skills/{skill_id}", json={"name": "Updated"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    async def test_delete_skill(self, auth_client):
        create = await auth_client.post("/api/skills", json={"name": "To Delete", "type": "skill"})
        skill_id = create.json()["id"]
        resp = await auth_client.delete(f"/api/skills/{skill_id}")
        assert resp.status_code == 204
        resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert resp.status_code == 404

    async def test_create_exceeds_content_size_limit(self, auth_client):
        from app.services.admin import update_admin_settings
        try:
            await update_admin_settings({"max_rule_size_kb": 1})
            resp = await auth_client.post("/api/skills", json={
                "name": "TooBig", "content": "x" * 2000, "type": "skill",
            })
            assert resp.status_code == 413
        finally:
            await update_admin_settings({"max_rule_size_kb": 25})

    async def test_create_strips_control_chars_in_content(self, auth_client):
        resp = await auth_client.post("/api/skills", json={
            "name": "Clean", "content": "hello\x00world", "type": "skill",
        })
        assert resp.status_code == 201
        assert resp.json()["content"] == "helloworld"


@pytest.mark.asyncio
class TestSkillAttachment:
    async def test_attach_and_detach(self, auth_client):
        skill_resp = await auth_client.post("/api/skills", json={"name": "Attach Test", "type": "skill"})
        skill_id = skill_resp.json()["id"]

        ep_resp = await auth_client.post("/api/endpoints", json={
            "name": "Test EP",
            "base_url": "https://example.com",
            "api_key": "test-key",
        })
        ep_id = ep_resp.json()["id"]

        attach_resp = await auth_client.post(f"/api/skills/{skill_id}/attach", json={"endpoint_id": ep_id})
        assert attach_resp.status_code == 201

        skill_resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert ep_id in skill_resp.json()["endpoints"]

        for_endpoint = await auth_client.get(f"/api/skills/for-endpoint/{ep_id}")
        assert for_endpoint.status_code == 200
        assert len(for_endpoint.json()["skills"]) == 1

        detach_resp = await auth_client.delete(f"/api/skills/{skill_id}/attach/{ep_id}")
        assert detach_resp.status_code == 204

        skill_resp = await auth_client.get(f"/api/skills/{skill_id}")
        assert ep_id not in skill_resp.json()["endpoints"]

    async def test_skills_do_not_leak_between_endpoints(self, auth_client):
        """A skill attached to one endpoint must not load for another.

        test_attach_and_detach only ever creates a single endpoint, so its
        `len(skills) == 1` assertion holds whether or not the query filters by
        endpoint at all. Two endpoints are needed to prove the filter exists.
        """
        endpoints = {}
        for name in ("Alpha", "Beta"):
            resp = await auth_client.post("/api/endpoints", json={
                "name": name, "base_url": "https://example.com", "api_key": "k",
            })
            endpoints[name] = resp.json()["id"]

        alpha_skill = (await auth_client.post(
            "/api/skills", json={"name": "Alpha Only", "content": "A", "type": "skill"}
        )).json()["id"]
        await auth_client.post(
            f"/api/skills/{alpha_skill}/attach", json={"endpoint_id": endpoints["Alpha"]}
        )

        for_alpha = (await auth_client.get(
            f"/api/skills/for-endpoint/{endpoints['Alpha']}")).json()["skills"]
        for_beta = (await auth_client.get(
            f"/api/skills/for-endpoint/{endpoints['Beta']}")).json()["skills"]

        assert [s["name"] for s in for_alpha] == ["Alpha Only"]
        assert for_beta == [], "skill attached to Alpha leaked into Beta"

    async def test_service_scopes_skills_to_the_requested_endpoint(self, auth_client):
        """Cover load_skills_for_endpoint, not just the API route.

        app/routers/skills.py:205 duplicates this query rather than calling the
        service, so an API-level test proves nothing about the service copy --
        and the service copy is the one on the proxy hot path.
        """
        from app.services.skills import load_skills_for_endpoint
        from tests.conftest import TestSessionLocal

        endpoints = {}
        for name in ("Alpha", "Beta"):
            resp = await auth_client.post("/api/endpoints", json={
                "name": name, "base_url": "https://example.com", "api_key": "k",
            })
            endpoints[name] = resp.json()["id"]

        skill_id = (await auth_client.post(
            "/api/skills", json={"name": "Alpha Only", "content": "ALPHA", "type": "skill"}
        )).json()["id"]
        sample_id = (await auth_client.post(
            "/api/skills", json={"name": "Alpha Sample", "content": "SAMPLE", "type": "sample"}
        )).json()["id"]
        for item in (skill_id, sample_id):
            await auth_client.post(
                f"/api/skills/{item}/attach", json={"endpoint_id": endpoints["Alpha"]}
            )

        me = (await auth_client.get("/api/auth/me")).json()

        async with TestSessionLocal() as db:
            alpha_skills, alpha_samples = await load_skills_for_endpoint(
                endpoints["Alpha"], me["id"], db
            )
            beta_skills, beta_samples = await load_skills_for_endpoint(
                endpoints["Beta"], me["id"], db
            )

        assert alpha_skills == ["ALPHA"]
        assert alpha_samples == ["SAMPLE"]
        assert beta_skills == [], "skill attached to Alpha leaked into Beta"
        assert beta_samples == [], "sample attached to Alpha leaked into Beta"

    async def test_skills_do_not_leak_between_users(self, auth_client, client):
        """load_skills_for_endpoint filters on user_id as well as endpoint_id."""
        from app.models.user import User
        from app.services.skills import load_skills_for_endpoint
        from tests.conftest import TestSessionLocal

        owner_ep = (await auth_client.post("/api/endpoints", json={
            "name": "Shared", "base_url": "https://example.com", "api_key": "k",
        })).json()["id"]
        skill_id = (await auth_client.post(
            "/api/skills", json={"name": "Private", "content": "SECRET", "type": "skill"}
        )).json()["id"]
        await auth_client.post(f"/api/skills/{skill_id}/attach", json={"endpoint_id": owner_ep})

        async with TestSessionLocal() as db:
            intruder = User(username="intruder", password_hash="x", gitv_api_key="k2")
            db.add(intruder)
            await db.commit()

            mine, _ = await load_skills_for_endpoint(owner_ep, intruder.id, db)
            assert mine == [], "another user's skill was loaded for this endpoint"


class TestSkillInjection:
    def test_inject_skills_into_system_message(self):
        from app.services.skills import inject_skills
        messages = [{"role": "system", "content": "You are a character."}]
        result = inject_skills(messages, ["Always write in third person."])
        assert "<skills>" in result[0]["content"]
        assert "Always write in third person." in result[0]["content"]

    def test_inject_skills_creates_system_if_none(self):
        from app.services.skills import inject_skills
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_skills(messages, ["Be descriptive."])
        assert result[0]["role"] == "system"
        assert "<skills>" in result[0]["content"]

    def test_inject_skills_empty_noop(self):
        from app.services.skills import inject_skills
        messages = [{"role": "system", "content": "Original"}]
        result = inject_skills(messages, [])
        assert result[0]["content"] == "Original"

    def test_inject_samples_before_last_message(self):
        from app.services.skills import inject_samples
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Write something"},
        ]
        result = inject_samples(messages, ["Style reference here"])
        sample_idx = next(i for i, m in enumerate(result) if m.get("content", "").startswith("<writing_sample>"))
        assert result[sample_idx]["role"] == "system"
        assert result[sample_idx + 1]["content"] == "Write something"

    def test_inject_samples_empty_noop(self):
        from app.services.skills import inject_samples
        messages = [{"role": "user", "content": "Hello"}]
        result = inject_samples(messages, [])
        assert len(result) == 1


@pytest.mark.asyncio
class TestSkillQueryOnUpgradedDatabase:
    """Regression guard for the 0.18.0 `no such column: skills.budget_weight` bug.

    0.18.0 added `budget_weight` to app/models/skill.py with no migration. Because
    `create_all` never alters an existing table, the column was present on fresh
    installs and absent on every upgrade, and `load_skills_for_endpoint` -- which
    runs on the proxy hot path -- failed with OperationalError for those users.

    Every other test in this file runs against a `create_all` database, so none of
    them could catch it. This one builds the skills tables the way an upgrade
    actually produces them: from migration 032's DDL, exactly as it shipped in
    0.15.x, with nothing else touching them.
    """

    @staticmethod
    async def _upgraded_engine():
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models import Base
        from app.services.migrations import MIGRATIONS, _resolve_sql, run_migrations

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Replace the model-built skills tables with the 0.15.x definitions.
            await conn.execute(text("DROP TABLE endpoint_skills;"))
            await conn.execute(text("DROP TABLE skills;"))
            sql = _resolve_sql(dict(MIGRATIONS)["032_create_skills_tables"], "sqlite")
            for statement in (s.strip() for s in sql.split(";")):
                if statement:
                    await conn.execute(text(statement))

        await run_migrations(engine)
        return engine

    async def test_old_skills_table_lacks_budget_weight_before_migration(self):
        """Prove the fixture really reproduces the broken state 042 repairs."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.models import Base
        from app.services.migrations import MIGRATIONS, _resolve_sql

        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE endpoint_skills;"))
                await conn.execute(text("DROP TABLE skills;"))
                sql = _resolve_sql(dict(MIGRATIONS)["032_create_skills_tables"], "sqlite")
                for statement in (s.strip() for s in sql.split(";")):
                    if statement:
                        await conn.execute(text(statement))
                cols = await conn.execute(text("PRAGMA table_info(skills);"))
                names = {row[1] for row in cols.fetchall()}
            assert "budget_weight" not in names, (
                "Fixture no longer reproduces the pre-0.18.0 schema; this test is vacuous"
            )
        finally:
            await engine.dispose()

    async def test_migration_042_adds_budget_weight(self):
        from sqlalchemy import text

        engine = await self._upgraded_engine()
        try:
            async with engine.begin() as conn:
                cols = await conn.execute(text("PRAGMA table_info(skills);"))
                info = {row[1]: row for row in cols.fetchall()}
            assert "budget_weight" in info, "Migration 042 did not add skills.budget_weight"
        finally:
            await engine.dispose()

    async def test_load_skills_for_endpoint_succeeds_after_upgrade(self):
        """The exact query from the reported OperationalError."""
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.models.endpoint import Endpoint
        from app.models.skill import EndpointSkill, Skill
        from app.models.user import User
        from app.services.skills import load_skills_for_endpoint

        engine = await self._upgraded_engine()
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                user = User(username="u", password_hash="x", gitv_api_key="k")
                db.add(user)
                await db.flush()
                endpoint = Endpoint(user_id=user.id, name="e", base_url="http://localhost")
                db.add(endpoint)
                await db.flush()
                skill = Skill(
                    user_id=user.id, name="s", description="", content="SKILL BODY", type="skill"
                )
                sample = Skill(
                    user_id=user.id, name="p", description="", content="SAMPLE BODY", type="sample"
                )
                db.add_all([skill, sample])
                await db.flush()
                db.add_all([
                    EndpointSkill(endpoint_id=endpoint.id, skill_id=skill.id),
                    EndpointSkill(endpoint_id=endpoint.id, skill_id=sample.id),
                ])
                await db.commit()

                skills, samples = await load_skills_for_endpoint(endpoint.id, user.id, db)

            assert skills == ["SKILL BODY"]
            assert samples == ["SAMPLE BODY"]
        finally:
            await engine.dispose()

    async def test_budget_weight_defaults_to_one_on_upgraded_rows(self):
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.models.skill import Skill
        from app.models.user import User

        engine = await self._upgraded_engine()
        try:
            session_factory = async_sessionmaker(engine, expire_on_commit=False)
            async with session_factory() as db:
                user = User(username="u", password_hash="x", gitv_api_key="k")
                db.add(user)
                await db.flush()
                db.add(Skill(user_id=user.id, name="s", description="", content="c", type="skill"))
                await db.commit()

                loaded = (await db.execute(select(Skill))).scalars().one()
                assert loaded.budget_weight == 1.0
        finally:
            await engine.dispose()
