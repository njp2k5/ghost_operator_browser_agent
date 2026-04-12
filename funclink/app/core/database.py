import re

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


def _build_asyncpg_url(raw: str) -> tuple[str, dict]:
    """Strip Neon-specific query params and return (clean_url, connect_args)."""
    # Remove sslmode and channel_binding from the URL — asyncpg uses connect_args instead
    clean = re.sub(r'[?&]sslmode=[^&]*', '', raw)
    clean = re.sub(r'[?&]channel_binding=[^&]*', '', clean)
    # Repair URL if we stripped the only query param (leftover ? or &)
    clean = re.sub(r'[?&]$', '', clean)
    needs_ssl = 'sslmode=require' in raw or 'neon.tech' in raw
    connect_args = {"ssl": "require"} if needs_ssl else {}
    return clean, connect_args


_db_url, _connect_args = _build_asyncpg_url(settings.DATABASE_URL)

engine = create_async_engine(
    _db_url,
    echo=False,
    pool_pre_ping=True,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables():
    """Create all tables on startup (used only in dev; use Alembic in prod)."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        async with engine.begin() as conn:
            from app.models import session as _  # noqa: ensure models are imported
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified/created.")
    except Exception as e:
        logger.warning(
            f"Could not connect to database on startup: {e}\n"
            "Set a valid DATABASE_URL in .env to enable DB features."
        )
