import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatType
from aiogram.fsm.storage.base import BaseEventIsolation, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message


class FloodGate:
    """Bound previews/commands as well as confirmed submissions, without network I/O."""

    def __init__(self):
        self.buckets: dict[int, tuple[float, float]] = {}
        self.last_purge = 0.0

    def purge(self):
        now = time.monotonic()
        self.buckets = {key: value for key, value in self.buckets.items() if now - value[1] < 300}
        self.last_purge = now

    def allow(self, user_id: int) -> bool:
        now = time.monotonic()
        if now - self.last_purge >= 60:
            self.purge()
        tokens, previous = self.buckets.get(user_id, (20.0, now))
        tokens = min(20.0, tokens + now - previous)
        allowed = tokens >= 1
        self.buckets[user_id] = (tokens - 1 if allowed else tokens, now)
        return allowed


class PrivateChatRequests(BaseRequestMiddleware):
    """Enforce the no-channel invariant at the last outgoing API boundary."""

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        if chat_id is not None and (not isinstance(chat_id, int) or chat_id <= 0):
            raise PermissionError("Only positive private chat IDs are allowed")
        return await make_request(bot, method)


class PrivateAccessMiddleware(BaseMiddleware):
    def __init__(self, service):
        self.service = service
        self.flood_gate = FloodGate()

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
            if isinstance(event, CallbackQuery):
                await event.answer("открой бота в личных сообщениях ♡", show_alert=True)
            return None
        user = event.from_user
        if user is None or user.is_bot:
            return None
        if message.chat.id != user.id:
            return None
        if not self.flood_gate.allow(user.id):
            # Silence repeated flood updates instead of amplifying them into Telegram API calls.
            return None
        if isinstance(event, CallbackQuery) and (event.data or "").startswith("a:"):
            if user.id not in self.service.settings.admin_ids:
                await event.answer("это действие доступно только модераторам", show_alert=True)
                return None
        data["service"] = self.service
        return await handler(event, data)


class ExpiringMemoryStorage(MemoryStorage):
    """Drafts are intentionally temporary and never become submissions without confirmation."""

    def __init__(self, ttl: int):
        super().__init__()
        self.ttl = ttl
        self.expires: dict[StorageKey, float] = {}

    def expire_key(self, key):
        if self.expires.get(key, 0) <= time.monotonic():
            self.storage.pop(key, None)
            self.expires.pop(key, None)

    def purge(self):
        for key in list(self.expires):
            self.expire_key(key)

    async def get_state(self, key):
        self.expire_key(key)
        if key not in self.storage:
            return None
        return await super().get_state(key)

    async def get_data(self, key):
        self.expire_key(key)
        if key not in self.storage:
            return {}
        return await super().get_data(key)

    async def set_state(self, key, state=None):
        await super().set_state(key, state)
        self.expires[key] = time.monotonic() + self.ttl

    async def set_data(self, key, data):
        await super().set_data(key, data)
        self.expires[key] = time.monotonic() + self.ttl

    async def close(self):
        self.storage.clear()
        self.expires.clear()


class UserEventIsolation(BaseEventIsolation):
    """Locks exist only while handlers are active or waiting; no unbounded lock cache."""

    def __init__(self):
        self.entries: dict[StorageKey, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        lock, count = self.entries.get(key, (asyncio.Lock(), 0))
        self.entries[key] = (lock, count + 1)
        try:
            async with lock:
                yield
        finally:
            _, count = self.entries[key]
            if count == 1:
                del self.entries[key]
            else:
                self.entries[key] = (lock, count - 1)

    async def close(self):
        self.entries.clear()
