import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import text

from love_bot.bot import make_bot, make_dispatcher
from love_bot.config import Settings, database_url
from love_bot.db import make_engine, make_sessions
from love_bot.service import SubmissionService

# Synthetic fixtures. Never use a real token in tests.
TEST_TOKEN = "123456:TEST_ONLY_TOKEN_NOT_VALID_FOR_TELEGRAM"
ADMIN = 900001
OTHER_ADMIN = 900002
AUTHOR = 100001
OTHER_AUTHOR = 100002


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        bot_token=TEST_TOKEN,
        database_url="postgresql+asyncpg://postgres:test@localhost/love_bot_test",
        admin_ids={ADMIN, OTHER_ADMIN},
        auto_approve_enabled=False,
    )


@pytest.fixture(scope="session")
def postgres_url():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL integration tests")
    url = database_url(value)
    if not url.database or not url.database.endswith("_test"):
        pytest.fail("TEST_DATABASE_URL must point to a dedicated database ending in _test")
    env = dict(os.environ, DATABASE_URL=value)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(
            "Test database migration failed (inspect credentials and database availability)"
        )
    return value


@pytest_asyncio.fixture
async def service(postgres_url, settings):
    engine = make_engine(postgres_url)
    async with engine.begin() as connection:
        # Guarded by postgres_url's explicit _test suffix; only this bot's schema is reset.
        await connection.execute(
            text(
                "TRUNCATE love_bot.publication_jobs, love_bot.publication_state, love_bot.notification_outbox, love_bot.moderation_audit, "
                "love_bot.submissions, love_bot.bot_users RESTART IDENTITY CASCADE"
            )
        )
        await connection.execute(text("INSERT INTO love_bot.publication_state (id) VALUES (1)"))
    configured = settings.model_copy(update={"database_url": settings.database_url})
    instance = SubmissionService(make_sessions(engine), configured)
    yield instance
    await engine.dispose()


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.message_id = 100
        self.bot_user = User(
            id=123456, is_bot=True, first_name="test bot", username="love_test_bot"
        )
        self.failure = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- aiogram API
        self.calls.append(method)
        if self.failure and isinstance(method, SendMessage):
            raise self.failure(method)
        if isinstance(method, GetMe):
            return self.bot_user
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText)):
            self.message_id += 1
            return Message(
                message_id=self.message_id,
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="channel" if method.chat_id < 0 else "private"),
                from_user=self.bot_user,
                text=method.text,
                reply_markup=method.reply_markup,
            )
        raise AssertionError(f"Unexpected Telegram API method: {type(method).__name__}")

    async def stream_content(
        self,
        url,
        headers=None,
        timeout=30,  # noqa: ASYNC109 -- aiogram API signature
        chunk_size=65536,
        raise_for_status=True,
    ):
        if False:
            yield b""


class Driver:
    def __init__(self, service):
        self.session = RecordingSession()
        self.bot = make_bot(service.settings, session=self.session)
        self.dp = make_dispatcher(service)
        self.sequence = 0

    def user(self, user_id):
        return User(id=user_id, is_bot=False, first_name="not stored")

    async def message(self, body, user_id=AUTHOR, chat_id=None, chat_type="private"):
        self.sequence += 1
        kwargs = (
            {"text": body}
            if body is not None
            else {
                "sticker": {
                    "file_id": "test",
                    "file_unique_id": "test",
                    "type": "regular",
                    "width": 1,
                    "height": 1,
                    "is_animated": False,
                    "is_video": False,
                }
            }
        )
        update = Update(
            update_id=self.sequence,
            message=Message(
                message_id=self.sequence,
                date=datetime.now(UTC),
                chat=Chat(id=chat_id or user_id, type=chat_type),
                from_user=self.user(user_id),
                **kwargs,
            ),
        )
        return await self.dp.feed_update(self.bot, update)

    async def callback(self, data, user_id=AUTHOR, chat_id=None, chat_type="private"):
        self.sequence += 1
        update = Update(
            update_id=self.sequence,
            callback_query=CallbackQuery(
                id=str(self.sequence),
                from_user=self.user(user_id),
                chat_instance="test",
                data=data,
                message=Message(
                    message_id=50,
                    date=datetime.now(UTC),
                    chat=Chat(id=chat_id or user_id, type=chat_type),
                    from_user=self.session.bot_user,
                    text="test card",
                ),
            ),
        )
        return await self.dp.feed_update(self.bot, update)

    def button(self, prefix):
        for method in reversed(self.session.calls):
            markup = getattr(method, "reply_markup", None)
            if markup:
                for row in markup.inline_keyboard:
                    for button in row:
                        if button.callback_data and button.callback_data.startswith(prefix):
                            return button.callback_data
        raise AssertionError(f"No button matching {prefix}")

    async def draft(self, body="люблю тебя", user_id=AUTHOR):
        await self.callback("u:propose", user_id)
        await self.message(body, user_id)
        return self.button("u:send:")


@pytest_asyncio.fixture
async def driver(service):
    instance = Driver(service)
    yield instance
    await instance.dp.storage.close()
    await instance.dp.fsm.events_isolation.close()
    await instance.bot.session.close()
