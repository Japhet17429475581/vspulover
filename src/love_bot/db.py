from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from love_bot.config import database_url


def make_engine(url):
    return create_async_engine(
        database_url(url),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"server_settings": {"timezone": "UTC"}, "command_timeout": 30},
    )


def make_sessions(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
