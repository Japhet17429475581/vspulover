"""Actionable startup errors without logging Telegram request payloads or secrets."""

import logging

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)

logger = logging.getLogger("love_bot.runtime")


class StartupFailure(RuntimeError):
    """A startup failure whose safe diagnostic has already been logged."""


def telegram_error_details(exc: TelegramAPIError) -> dict[str, str]:
    # Never emit exc.message/str(exc), method.model_dump() or a traceback. They can
    # include user text, URLs, a token or other values copied from a request.
    message = exc.message.casefold()
    code = "telegram_request_rejected"
    hint = "Проверь настройки Telegram и указанный в логе этап запуска."
    cases = [
        (
            ("chat not found", "channel_invalid", "peer_id_invalid"),
            "chat_not_found",
            "Проверь CHANNEL_ID: нужен полный отрицательный ID канала. Добавь в канал именно бота с BOT_TOKEN этого хостинга.",
        ),
        (
            (
                "bot is not a member",
                "user not found",
                "member list is inaccessible",
                "participant_id_invalid",
            ),
            "bot_not_in_channel",
            "Добавь бота с BOT_TOKEN этого хостинга в администраторы указанного канала.",
        ),
        (
            (
                "not enough rights",
                "not an administrator",
                "administrator rights",
                "chat_admin_required",
            ),
            "channel_permission_denied",
            "Назначь бота администратором канала и включи право публикации сообщений.",
        ),
        (
            ("bot was kicked", "channel_private", "bot was blocked"),
            "channel_access_denied",
            "Проверь доступ бота к каналу, сними блокировку и назначь его администратором.",
        ),
        (
            ("bot_command_description_invalid", "command description"),
            "invalid_command_description",
            "Telegram отклонил описание команды. Обнови код; ручные команды работают и без меню.",
        ),
        (
            ("bot_commands_invalid", "bot_command_invalid", "commands are invalid"),
            "invalid_commands",
            "Telegram отклонил меню команд. Обнови код; ручные команды работают и без меню.",
        ),
        (
            ("webhook",),
            "webhook_error",
            "Не удалось отключить webhook. Проверь подключение к Telegram и повтори запуск.",
        ),
    ]
    for patterns, candidate, explanation in cases:
        if any(pattern in message for pattern in patterns):
            code, hint = candidate, explanation
            break
    if isinstance(exc, TelegramUnauthorizedError):
        code, hint = (
            "invalid_bot_token",
            "Проверь BOT_TOKEN в окружении хостинга и актуальный токен в BotFather.",
        )
    elif isinstance(exc, TelegramNetworkError):
        code, hint = (
            "telegram_network_error",
            "Хостинг не смог связаться с Telegram. Проверь сеть и повтори запуск.",
        )
    elif isinstance(exc, TelegramRetryAfter):
        code, hint = (
            "telegram_rate_limit",
            "Telegram ограничил частоту запросов. Подожди перед следующим запуском.",
        )
    elif isinstance(exc, TelegramForbiddenError) and code == "telegram_request_rejected":
        code, hint = (
            "telegram_access_denied",
            "Проверь доступ бота к каналу и его права администратора.",
        )
    return {
        "error_type": type(exc).__name__,
        "telegram_method": type(exc.method).__name__,
        "telegram_error": code,
        "hint": hint,
    }


async def startup_call(stage: str, request, *, required: bool = True):
    try:
        return await request
    except TelegramAPIError as exc:
        details = telegram_error_details(exc) | {"stage": stage}
        if not required:
            logger.warning("telegram_startup_optional_failed", extra=details)
            return None
        logger.error("telegram_startup_failed", extra=details)
        raise StartupFailure(stage) from None
