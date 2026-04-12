import asyncio
import os
from logging.config import fileConfig

from dotenv import load_dotenv
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Load .env so DATABASE_URL is available
load_dotenv()

# Alembic Config object
config = context.config

# Set up Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Import all models so Alembic can autogenerate migrations
from app.core.database import Base  # noqa
import app.models.session  # noqa — registers Session, Step, TaskMemory

target_metadata = Base.metadata

# Pull DATABASE_URL from environment
DB_URL = os.environ["DATABASE_URL"]

# Build psycopg2 sync URL for Alembic offline/autogenerate:
# - swap +asyncpg → +psycopg2
# - strip channel_binding (not supported by psycopg2)
import re as _re
_sync_url = DB_URL.replace("+asyncpg", "+psycopg2")
_sync_url = _re.sub(r'[?&]channel_binding=[^&]*', '', _sync_url)
_sync_url = _re.sub(r'[?&]$', '', _sync_url)

# Alembic offline mode needs a sync URL
config.set_main_option("sqlalchemy.url", _sync_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # Strip sslmode/channel_binding from asyncpg URL; pass ssl via connect_args
    _async_url = _re.sub(r'[?&]sslmode=[^&]*', '', DB_URL)
    _async_url = _re.sub(r'[?&]channel_binding=[^&]*', '', _async_url)
    _async_url = _re.sub(r'[?&]$', '', _async_url)
    _needs_ssl = 'sslmode=require' in DB_URL or 'neon.tech' in DB_URL
    connectable = async_engine_from_config(
        {"sqlalchemy.url": _async_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"ssl": "require"} if _needs_ssl else {},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
