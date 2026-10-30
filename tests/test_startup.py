import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.methods import DeleteWebhook, GetChat, GetMe, SendMessage, SetMyCommands

from love_bot.__main__ import prepare_telegram
from love_bot.diagnostics import StartupFailure, startup_call, telegram_error_details
from love_bot.logging_setup import JsonFormatter
from love_bot.publication import PublicationWorker


@pytest.mark.parametrize(
    ("error", "message", "expected"),
    [
        (TelegramBadRequest, "Bad Request: chat not found", "chat_not_found"),
        (TelegramBadRequest, "Bad Request: user not found", "bot_not_in_channel"),
        (TelegramBadRequest, "Bad Request: not enough rights", "channel_permission_denied"),
        (TelegramBadRequest, "BOT_COMMANDS_INVALID", "invalid_commands"),
        (TelegramBadRequest, "BOT_COMMAND_DESCRIPTION_INVALID", "invalid_command_description"),
        (TelegramBadRequest, "unexpected failure", "telegram_request_rejected"),
        (TelegramForbiddenError, "bot was kicked", "channel_access_denied"),
        (TelegramUnauthorizedError, "Unauthorized", "invalid_bot_token"),
        (TelegramNetworkError, "connection failed", "telegram_network_error"),
    ],
)
def test_error_classification_never_exposes_telegram_payload(error, message, expected):
    secret = "PRIVATE_TEXT_AND_TOKEN_SHOULD_NOT_APPEAR"
    exc = error(method=SendMessage(chat_id=123, text=secret), message=message + " " + secret)
    details = telegram_error_details(exc)
    assert details["telegram_error"] == expected
    assert details["telegram_method"] == "SendMessage"
    assert secret not in json.dumps(details)
    assert details["hint"]


async def test_required_failure_logs_stage_and_actionable_hint(caplog):
    failure = TelegramBadRequest(
        method=GetChat(chat_id=-1003475462009), message="Bad Request: chat not found"
    )
    request = AsyncMock(side_effect=failure)
    with caplog.at_level(logging.ERROR), pytest.raises(StartupFailure):
        await startup_call("get_publication_channel", request())
    record = caplog.records[-1]
    data = json.loads(JsonFormatter().format(record))
    assert data["stage"] == "get_publication_channel"
    assert data["telegram_method"] == "GetChat"
    assert data["telegram_error"] == "chat_not_found"
    assert "CHANNEL_ID" in data["hint"]


def fake_bot():
    return SimpleNamespace(
        id=123456,
        me=AsyncMock(return_value=SimpleNamespace(id=123456)),
        set_my_commands=AsyncMock(return_value=True),
        delete_webhook=AsyncMock(return_value=True),
    )


async def test_menu_error_does_not_stop_startup(caplog):
    bot = fake_bot()
    publisher = SimpleNamespace(validate_channel=AsyncMock())
    bot.set_my_commands.side_effect = TelegramBadRequest(
        method=SetMyCommands(commands=[]), message="BOT_COMMANDS_INVALID"
    )
    await prepare_telegram(bot, publisher)
    publisher.validate_channel.assert_awaited_once()
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
    warnings = [r for r in caplog.records if r.getMessage() == "telegram_startup_optional_failed"]
    assert len(warnings) == 1 and warnings[0].stage == "register_commands"


async def test_invalid_token_stops_before_channel_requests():
    bot = fake_bot()
    bot.me.side_effect = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    publisher = SimpleNamespace(validate_channel=AsyncMock())
    with pytest.raises(StartupFailure):
        await prepare_telegram(bot, publisher)
    publisher.validate_channel.assert_not_awaited()
    bot.set_my_commands.assert_not_awaited()


async def test_webhook_failure_is_reported_as_required_stage(caplog):
    bot = fake_bot()
    bot.delete_webhook.side_effect = TelegramBadRequest(
        method=DeleteWebhook(), message="Bad Request: unknown"
    )
    publisher = SimpleNamespace(validate_channel=AsyncMock())
    with pytest.raises(StartupFailure):
        await prepare_telegram(bot, publisher)
    assert caplog.records[-1].stage == "delete_webhook"


async def test_inaccessible_channel_reports_exact_method(settings, caplog):
    settings.publication_enabled = True
    settings.channel_id = -1003475462009
    bot = fake_bot()
    bot.get_chat = AsyncMock(
        side_effect=TelegramBadRequest(
            method=GetChat(chat_id=settings.channel_id), message="Bad Request: chat not found"
        )
    )
    bot.get_chat_member = AsyncMock()
    worker = PublicationWorker(SimpleNamespace(sessions=None, settings=settings), bot)
    with pytest.raises(StartupFailure):
        await worker.validate_channel()
    assert caplog.records[-1].stage == "get_publication_channel"
    assert caplog.records[-1].telegram_error == "chat_not_found"
    bot.get_chat_member.assert_not_awaited()
