import logging
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.services.migrations import run_migrations

logger = logging.getLogger(__name__)


def _ensure_data_dir() -> None:
    """Ensure the SQLite database directory exists before engine creation."""
    if "sqlite" in settings.database_url:
        db_part = settings.database_url.split("///")[-1]
        db_path = Path(db_part)
        db_path.parent.mkdir(parents=True, exist_ok=True)


_ensure_data_dir()

engine = create_async_engine(settings.database_url, echo=settings.debug)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    from app.models import Base  # noqa: F811

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations(engine)
    logger.info("Database initialized with migrations")
