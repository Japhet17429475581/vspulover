import asyncio
import logging
import sys
import time

from aiogram.types import BotCommand
from pydantic import ValidationError
from sqlalchemy import text

from love_bot.bot import make_bot, make_dispatcher
from love_bot.config import Settings
from love_bot.db import make_engine, make_sessions
from love_bot.logging_setup import configure_logging
from love_bot.notifications import NotificationWorker
from love_bot.service import SubmissionService

logger = logging.getLogger("love_bot.runtime")


async def housekeeping(service, dp, stop):
    next_cleanup = 0.0
    while not stop.is_set():
        dp.storage.purge()
        dp["private_access"].flood_gate.purge()
        if time.monotonic() >= next_cleanup:
            try:
                await service.cleanup()
            except Exception as exc:
                logger.error("cleanup_failed", extra={"error_type": type(exc).__name__})
            next_cleanup = time.monotonic() + 3600
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except TimeoutError:
            pass


async def run(settings: Settings):
    engine = make_engine(settings.database_url)
    bot = None
    tasks = []
    stop = asyncio.Event()
    try:
        # A dedicated connection keeps the advisory lock throughout the polling process.
        async with engine.connect() as connection:
            version = await connection.scalar(
                text("SELECT version_num FROM love_bot.alembic_version")
            )
            if version != "0001":
                raise RuntimeError("Run alembic upgrade head before starting the bot")
            locked = await connection.scalar(text("SELECT pg_try_advisory_lock(762950131)"))
            await connection.commit()
            if not locked:
                raise RuntimeError("Another bot instance is already running for this schema")
            try:
                service = SubmissionService(make_sessions(engine), settings)
                dp = make_dispatcher(service)
                bot = make_bot(settings)
                await bot.set_my_commands(
                    [
                        BotCommand(command="start", description="предложить что-то тёплое ♡"),
                        BotCommand(command="cancel", description="отменить действие"),
                        BotCommand(command="privacy", description="данные и сроки хранения"),
                        BotCommand(command="delete_my_data", description="удалить мои данные"),
                        BotCommand(command="admin", description="для модераторов"),
                    ]
                )
                tasks = [
                    asyncio.create_task(NotificationWorker(service, bot).run(stop)),
                    asyncio.create_task(housekeeping(service, dp, stop)),
                ]
                logger.info("bot_started")
                await bot.delete_webhook(drop_pending_updates=False)
                await dp.start_polling(
                    bot,
                    allowed_updates=dp.resolve_used_update_types(),
                    close_bot_session=False,
                    tasks_concurrency_limit=50,
                )
            finally:
                stop.set()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                await connection.execute(text("SELECT pg_advisory_unlock(762950131)"))
                await connection.commit()
    finally:
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def main():
    configure_logging("INFO")
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        asyncio.run(run(settings))
    except ValidationError as exc:
        # ValidationError.__str__ can contain input values, so only expose field names.
        fields = ", ".join(".".join(map(str, error["loc"])) for error in exc.errors())
        logger.error("configuration_invalid", extra={"error_type": fields})
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.critical("startup_or_runtime_failed", extra={"error_type": type(exc).__name__})
        sys.exit(1)


if __name__ == "__main__":
    main()
