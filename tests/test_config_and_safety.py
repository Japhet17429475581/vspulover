import asyncio
import logging
import time

import pytest
from aiogram.fsm.storage.base import StorageKey
from conftest import TEST_TOKEN
from pydantic import ValidationError

from love_bot.config import Settings, database_url
from love_bot.logging_setup import JsonFormatter
from love_bot.safety import ExpiringMemoryStorage, FloodGate, UserEventIsolation


@pytest.mark.parametrize("ids", ["123,456", "[123, 456]", [123, 456], {123, 456}])
def test_admin_id_formats(ids):
    settings = Settings(
        _env_file=None,
        bot_token=TEST_TOKEN,
        database_url="postgres://u:p@localhost/bot",
        admin_ids=ids,
    )
    assert settings.admin_ids == {123, 456}
    assert settings.auto_approve_enabled is False
    assert settings.auto_approve_confidence == 0.95
    assert "p@localhost" not in repr(settings)


@pytest.mark.parametrize("ids", ["", "-100123", "0", "hello"])
def test_admin_id_validation(ids):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            bot_token=TEST_TOKEN,
            database_url="postgres://u:p@localhost/bot",
            admin_ids=ids,
        )


def test_url_normalization_does_not_expose_password():
    url = database_url("postgres://u:p%40ss@localhost/db?sslmode=require")
    assert url.drivername == "postgresql+asyncpg" and url.password == "p@ss"
    assert url.query["ssl"] == "require" and "sslmode" not in url.query
    with pytest.raises(ValueError):
        database_url("sqlite:///test.db")


async def test_expiring_storage_and_temporary_locks():
    key = StorageKey(bot_id=1, chat_id=2, user_id=2)
    storage = ExpiringMemoryStorage(ttl=60)
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}
    assert not storage.storage  # Merely reading an unused state does not retain personal IDs.
    await storage.set_state(key, "confirming")
    await storage.set_data(key, {"text": "private draft"})
    storage.expires[key] = time.monotonic() - 1
    assert await storage.get_data(key) == {}
    assert await storage.get_state(key) is None
    isolation = UserEventIsolation()
    order = []

    async def task(number):
        async with isolation.lock(key):
            order.append(number)
            await asyncio.sleep(0)
            order.append(number)

    await asyncio.gather(task(1), task(2))
    assert order in ([1, 1, 2, 2], [2, 2, 1, 1])
    assert not isolation.entries
    await storage.close()


def test_log_formatter_discards_library_exception_payloads():
    record = logging.LogRecord(
        "sqlalchemy.engine", logging.ERROR, "test", 1, "secret text password", (), None
    )
    rendered = JsonFormatter().format(record)
    assert "secret text" not in rendered and "password" not in rendered
    assert "library_event" in rendered


def test_flood_gate_refills_and_cleans_ids(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("love_bot.safety.time.monotonic", lambda: clock[0])
    gate = FloodGate()
    assert all(gate.allow(42) for _ in range(20))
    assert not gate.allow(42)
    assert gate.allow(43)
    clock[0] += 1
    assert gate.allow(42)
    assert not gate.allow(42)
    clock[0] += 301
    gate.purge()
    assert not gate.buckets
