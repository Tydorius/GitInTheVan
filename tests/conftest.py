import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.services.admin as _admin_module
import app.services.assistant.admin_reads as _assistant_admin_reads_module
import app.services.assistant.llm as _assistant_llm_module
import app.services.assistant.loop as _assistant_loop_module
import app.services.assistant.selfcheck as _assistant_selfcheck_module
import app.services.assistant.store as _assistant_store_module
import app.services.backup as _backup_module
import app.services.budget as _budget_module
import app.services.bypass as _bypass_module
import app.services.cantrip as _cantrip_module
import app.services.command_tags as _command_tags_module
import app.services.conversation as _conversation_module
import app.services.debug as _debug_module
import app.services.debug_replay as _debug_replay_module
import app.services.debug_sandbox as _debug_sandbox_module
import app.services.driver_callable as _driver_callable_module
import app.services.forbidden_words as _forbidden_words_module
import app.services.map_pipeline as _map_pipeline_module
import app.services.memory as _memory_module
import app.services.proxy as _proxy_module
import app.services.scenario_summarizer as _scenario_summarizer_module
import app.services.summarization as _summarization_module
import app.services.verification as _verification_module
from app.config import settings as app_settings
from app.database import get_db
from app.main import app
from app.models.base import Base

test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

# Every service module that binds `async_session` at import time needs its
# binding swapped for the test session and restored afterwards. This was two
# hand-maintained lists; a module added to one and not the other failed at query
# time with "no such table", a long way from the cause. One list now drives both
# directions, so adding a service means adding a single line here.
_SESSION_MODULES = [
    _admin_module,
    _assistant_admin_reads_module,
    _assistant_llm_module,
    _assistant_loop_module,
    _assistant_selfcheck_module,
    _assistant_store_module,
    _backup_module,
    _budget_module,
    _bypass_module,
    _cantrip_module,
    _command_tags_module,
    _conversation_module,
    _debug_module,
    _debug_replay_module,
    _debug_sandbox_module,
    _driver_callable_module,
    _forbidden_words_module,
    _map_pipeline_module,
    _memory_module,
    _proxy_module,
    _scenario_summarizer_module,
    _summarization_module,
    _verification_module,
]

_ORIGINAL_SESSIONS = {m: m.async_session for m in _SESSION_MODULES}


async def override_get_db():
    async with TestSessionLocal() as session:
        yield session


app.dependency_overrides[get_db] = override_get_db

app_settings.rate_limit_enabled = False


@pytest.fixture(autouse=True)
async def setup_database():
    for module in _SESSION_MODULES:
        module.async_session = TestSessionLocal
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    for module, original in _ORIGINAL_SESSIONS.items():
        module.async_session = original
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(autouse=True)
def reset_updater_caches():
    """Clear the updater's process-lifetime caches between tests.

    The release list and detected version are cached for the life of the
    process (each update hop is a fresh process, so that is correct in
    production). Left alone in a test run they leak across tests, and
    httpx_mock asserts every registered response was consumed -- a cached
    release list silently swallows one.
    """
    from app.services.updater import _clear_releases_cache, _clear_version_cache

    _clear_releases_cache()
    _clear_version_cache()
    yield
    _clear_releases_cache()
    _clear_version_cache()


@pytest.fixture(autouse=True)
def reset_cert_ip_check():
    """Clear the cert/LAN-address check's process-lifetime state between tests.

    The acknowledgement and the served-certificate snapshot are deliberately
    process-scoped in production (a restart re-raises the warning), so left
    alone they leak across tests.
    """
    from app.services.ssl_manager import reset_cert_ip_check_state

    reset_cert_ip_check_state()
    yield
    reset_cert_ip_check_state()


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
