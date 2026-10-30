import asyncio
import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import create_async_engine

from love_bot.config import database_url
from love_bot.models import SCHEMA, Base

load_dotenv()
target_metadata = Base.metadata


def get_url():
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise RuntimeError("Задайте DATABASE_URL перед запуском миграций")
    return database_url(raw)


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name == SCHEMA
    return True


def run_migrations(connection):
    # Version table is isolated too; never inspect or alter other projects' tables.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(get_url(), poolclass=pool.NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            # Serialize migration runners, including creation of the version table.
            await connection.execute(text("SELECT pg_advisory_lock(762950130)"))
            try:
                await connection.execute(text('CREATE SCHEMA IF NOT EXISTS "love_bot"'))
                await connection.commit()
                await connection.run_sync(run_migrations)
            finally:
                await connection.execute(text("SELECT pg_advisory_unlock(762950130)"))
                await connection.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.execute('CREATE SCHEMA IF NOT EXISTS "love_bot"')
        context.run_migrations()
else:
    asyncio.run(run_online())
