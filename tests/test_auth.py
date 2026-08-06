import pytest

from app.services.auth import decode_access_token


@pytest.mark.asyncio
async def test_setup_creates_admin(client):
    resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "access_token" in data
    assert "api_key" in data
    assert data["api_key"].startswith("gitv_")

    payload = decode_access_token(data["access_token"])
    assert payload is not None
    assert payload["username"] == "admin"
    assert payload["is_admin"] is True


@pytest.mark.asyncio
async def test_setup_fails_if_admin_exists(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/auth/setup",
        json={"username": "another_admin", "password": "pass123"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_login_success(admin_client):
    client, _, _ = admin_client
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


@pytest.mark.asyncio
async def test_login_wrong_password(client, admin_client):
    _, _, _ = admin_client
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrongpassword"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client):
    resp = await client.post(
        "/api/auth/login",
        json={"username": "nobody", "password": "pass"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
class TestAdminAuthorization:
    """Cover require_admin against an *authenticated non-admin*.

    Existing admin-route tests use an unauthenticated client, which is rejected
    by get_current_user with a 401 before require_admin is ever consulted -- so
    the admin gate itself had no coverage. Mutation testing confirmed it:
    removing the `is_admin` check entirely left the suite green. That check is
    the only thing stopping any registered user from reading audit logs,
    rotating SSL certs, or triggering an update.
    """

    @pytest.fixture
    async def member_client(self, admin_client):
        """A logged-in ordinary user. POST /api/users always creates is_admin=False."""
        client, _, _ = admin_client
        created = await client.post(
            "/api/users", json={"username": "member", "password": "memberpass123"}
        )
        assert created.status_code == 201
        assert created.json()["is_admin"] is False

        client.headers.pop("Authorization", None)
        login = await client.post(
            "/api/auth/login", json={"username": "member", "password": "memberpass123"}
        )
        assert login.status_code == 200
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        yield client
        client.headers.pop("Authorization", None)

    async def test_token_is_valid_but_not_admin(self, member_client):
        """Guards the fixture: the request must be authenticated, or 403 is vacuous."""
        me = await member_client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["is_admin"] is False

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/admin/settings"),
        ("get", "/api/admin/update/check"),
        ("post", "/api/admin/update/execute"),
        ("get", "/api/admin/update/chain"),
        ("delete", "/api/admin/update/chain"),
        ("post", "/api/admin/update/chain/resume"),
        ("get", "/api/admin/schema-repair"),
        ("get", "/api/admin/ssl/status"),
        ("get", "/api/users"),
    ])
    async def test_non_admin_is_forbidden(self, member_client, method, path):
        resp = await getattr(member_client, method)(path)
        assert resp.status_code == 403, (
            f"{method.upper()} {path} returned {resp.status_code} for a non-admin user"
        )

    async def test_audit_log_is_per_user_not_admin_only(self, member_client):
        """/api/audit is deliberately not an admin route.

        It is scoped to the caller's own logs (app/routers/audit.py passes
        current_user.id to list_logs), so a 200 here is correct. What matters is
        that the scoping holds -- a user must never see another user's entries.
        """
        resp = await member_client.get("/api/audit")
        assert resp.status_code == 200

        from app.services.audit import list_logs
        from tests.conftest import TestSessionLocal

        me = (await member_client.get("/api/auth/me")).json()["id"]
        async with TestSessionLocal() as db:
            from app.models.audit_log import AuditLog

            db.add(AuditLog(user_id="someone-else", action="login", target_type="user"))
            db.add(AuditLog(user_id=me, action="login", target_type="user"))
            await db.commit()

            mine = await list_logs(db, me)

        assert len(mine) == 1
        assert all(log["user_id"] == me for log in mine), "audit logs leaked across users"
