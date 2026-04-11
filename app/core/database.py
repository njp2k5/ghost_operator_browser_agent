from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
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
