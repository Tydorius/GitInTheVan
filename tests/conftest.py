import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.admin as _admin_module
import app.services.budget as _budget_module
import app.services.bypass as _bypass_module
import app.services.cantrip as _cantrip_module
import app.services.command_tags as _command_tags_module
import app.services.conversation as _conversation_module
import app.services.debug as _debug_module
import app.services.driver_callable as _driver_callable_module
import app.services.forbidden_words as _forbidden_words_module
import app.services.memory as _memory_module
import app.services.proxy as _proxy_module
import app.services.summarization as _summarization_module
import app.services.verification as _verification_module
from app.config import settings as app_settings
from app.database import get_db
from app.main import app
from app.models.base import Base

test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

_original_proxy_session = _proxy_module.async_session
_original_cantrip_session = _cantrip_module.async_session
_original_memory_session = _memory_module.async_session
_original_verification_session = _verification_module.async_session
_original_conversation_session = _conversation_module.async_session
_original_summarization_session = _summarization_module.async_session
_original_forbidden_words_session = _forbidden_words_module.async_session
_original_driver_callable_session = _driver_callable_module.async_session
_original_command_tags_session = _command_tags_module.async_session
_original_bypass_session = _bypass_module.async_session
_original_budget_session = _budget_module.async_session
_original_debug_session = _debug_module.async_session
_original_admin_session = _admin_module.async_session


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db

app_settings.rate_limit_enabled = False


@pytest.fixture(autouse=True)
async def setup_database():
    _proxy_module.async_session = TestSessionLocal
    _cantrip_module.async_session = TestSessionLocal
    _memory_module.async_session = TestSessionLocal
    _conversation_module.async_session = TestSessionLocal
    _verification_module.async_session = TestSessionLocal
    _summarization_module.async_session = TestSessionLocal
    _forbidden_words_module.async_session = TestSessionLocal
    _driver_callable_module.async_session = TestSessionLocal
    _command_tags_module.async_session = TestSessionLocal
    _bypass_module.async_session = TestSessionLocal
    _budget_module.async_session = TestSessionLocal
    _debug_module.async_session = TestSessionLocal
    _admin_module.async_session = TestSessionLocal
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    _proxy_module.async_session = _original_proxy_session
    _cantrip_module.async_session = _original_cantrip_session
    _memory_module.async_session = _original_memory_session
    _conversation_module.async_session = _original_conversation_session
    _verification_module.async_session = _original_verification_session
    _summarization_module.async_session = _original_summarization_session
    _forbidden_words_module.async_session = _original_forbidden_words_session
    _driver_callable_module.async_session = _original_driver_callable_session
    _command_tags_module.async_session = _original_command_tags_session
    _bypass_module.async_session = _original_bypass_session
    _budget_module.async_session = _original_budget_session
    _debug_module.async_session = _original_debug_session
    _admin_module.async_session = _original_admin_session
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def admin_client(client):
    setup_resp = await client.post(
        "/api/auth/setup",
        json={"username": "admin", "password": "adminpass123"},
    )
    assert setup_resp.status_code == 201
    token = setup_resp.json()["access_token"]
    api_key = setup_resp.json()["api_key"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client, token, api_key
    client.headers.pop("Authorization", None)
