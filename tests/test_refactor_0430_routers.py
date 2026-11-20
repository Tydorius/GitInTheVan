"""Unit tests for shared router helpers in app.routers._helpers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.memory_rule import MemoryRule
from app.models.user import User
from app.routers._helpers import check_tag_unique, get_owned_or_404


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(MemoryRule.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all([
            User(id="u1", username="alice", password_hash="x", gitv_api_key="k1"),
            User(id="u2", username="bob", password_hash="x", gitv_api_key="k2"),
            MemoryRule(id="r1", user_id="u1", name="Rule 1", tag="alpha"),
            MemoryRule(id="r2", user_id="u1", name="Rule 2", tag=""),
            MemoryRule(id="r3", user_id="u2", name="Bob Rule", tag="alpha"),
        ])
        await session.commit()
        yield session
    await engine.dispose()


@pytest.mark.asyncio
class TestCheckTagUnique:
    async def test_empty_tag_allowed(self, db_session: AsyncSession):
        await check_tag_unique(db_session, MemoryRule, "u1", "")

    async def test_unique_tag_allowed(self, db_session: AsyncSession):
        await check_tag_unique(db_session, MemoryRule, "u1", "beta")

    async def test_same_tag_different_user_allowed(self, db_session: AsyncSession):
        await check_tag_unique(db_session, MemoryRule, "u2", "beta")

    async def test_duplicate_tag_raises_409(self, db_session: AsyncSession):
        with pytest.raises(HTTPException) as exc_info:
            await check_tag_unique(db_session, MemoryRule, "u1", "alpha")
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail == "Tag already in use"

    async def test_self_exclude_allowed(self, db_session: AsyncSession):
        await check_tag_unique(db_session, MemoryRule, "u1", "alpha", exclude_id="r1")


@pytest.mark.asyncio
class TestGetOwnedOr404:
    async def test_owned_returns_object(self, db_session: AsyncSession):
        obj = await get_owned_or_404(db_session, MemoryRule, "r1", "u1", "Memory rule")
        assert obj.id == "r1"
        assert obj.name == "Rule 1"

    async def test_missing_raises_404(self, db_session: AsyncSession):
        with pytest.raises(HTTPException) as exc_info:
            await get_owned_or_404(db_session, MemoryRule, "missing", "u1", "Memory rule")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Memory rule not found"

    async def test_foreign_raises_404(self, db_session: AsyncSession):
        with pytest.raises(HTTPException) as exc_info:
            await get_owned_or_404(db_session, MemoryRule, "r3", "u1", "Memory rule")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Memory rule not found"
