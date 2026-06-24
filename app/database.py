import logging
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.services.migrations import run_migrations

logger = logging.getLogger(__name__)


def _ensure_data_dir() -> None:
    """Ensure the SQLite database directory exists before engine creation."""
    if settings.database_url.startswith("sqlite"):
        db_part = settings.database_url.split("///")[-1]
        db_path = Path(db_part)
        if not str(db_path).startswith(":"):
            db_path.parent.mkdir(parents=True, exist_ok=True)


def _is_sqlite() -> bool:
    return settings.database_url.startswith("sqlite")


def _create_engine():
    url = settings.database_url
    kwargs: dict = {"echo": settings.debug}

    if not _is_sqlite():
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,
            pool_recycle=settings.db_pool_recycle,
        )

    return create_async_engine(url, **kwargs)


_ensure_data_dir()
engine = _create_engine()

if _is_sqlite():
    file_db = ":///" in settings.database_url and ":memory:" not in settings.database_url

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        """Enable WAL mode and foreign keys for SQLite file databases."""
        if file_db:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()


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
