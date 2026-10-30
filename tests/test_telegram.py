import asyncio

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from conftest import ADMIN, AUTHOR, OTHER_ADMIN, OTHER_AUTHOR
from sqlalchemy import select

from love_bot.models import BotUser, Notification
from love_bot.notifications import NotificationWorker
from love_bot.service import SubmissionService

pytestmark = pytest.mark.postgres


async def test_full_approval_path(driver, service, caplog):
    await driver.message("/start")
    send_button = await driver.draft("люблю тебя, даже если ты об этом не знаешь")
    assert not await service.mine(AUTHOR)  # Preview never becomes a submitted record.
    assert any(
        "\n\nтебя, даже если" in (getattr(m, "text", "") or "") for m in driver.session.calls
    )
    await asyncio.gather(driver.callback(send_button), driver.callback(send_button))
    submissions = await service.mine(AUTHOR)
    assert len(submissions) == 1 and submissions[0].status == "pending"
    await driver.message("/admin", ADMIN)
    await driver.callback("a:q:pending:0", ADMIN)
    await driver.callback(driver.button("a:ok:"), ADMIN)
    stored = (await service.mine(AUTHOR))[0]
    assert stored.status == "approved" and stored.reviewed_by == ADMIN
    await driver.callback(send_button)
    assert len(await service.mine(AUTHOR)) == 1
    worker = NotificationWorker(service, driver.bot)
    for _ in range(10):
        if not await worker.process_once():
            break
    assert any(
        isinstance(m, SendMessage)
        and m.chat_id == AUTHOR
        and m.text == "твоё предложение одобрено ♡"
        for m in driver.session.calls
    )
    assert all(
        isinstance(m.chat_id, int) and m.chat_id > 0
        for m in driver.session.calls
        if hasattr(m, "chat_id")
    )
    cards = [
        m.text
        for m in driver.session.calls
        if isinstance(m, EditMessageText) and "Статус:" in m.text
    ]
    assert cards and all(str(AUTHOR) not in text and "not stored" not in text for text in cards)
    assert not [r for r in caplog.records if r.levelno >= 40]


async def test_full_rejection_path_and_competing_callbacks(driver, service):
    button = await driver.draft("люблю преподавателя")
    await driver.callback(button)
    submission = (await service.mine(AUTHOR))[0]
    await driver.callback(f"a:no:{submission.id}", ADMIN)
    reason = driver.button(f"a:reject:{submission.id}:personal")
    await asyncio.gather(
        driver.callback(reason, ADMIN), driver.callback(f"a:ok:{submission.id}", OTHER_ADMIN)
    )
    stored = (await service.mine(AUTHOR))[0]
    assert stored.status in {"approved", "rejected"}
    assert stored.reviewed_by in {ADMIN, OTHER_ADMIN}


async def test_rejection_notification_is_neutral(driver, service):
    await driver.callback(await driver.draft("его привычку опаздывать"))
    submission = (await service.mine(AUTHOR))[0]
    await driver.callback(f"a:reject:{submission.id}:format", ADMIN)
    assert (await service.mine(AUTHOR))[0].status == "rejected"
    worker = NotificationWorker(service, driver.bot)
    while await worker.process_once():
        pass
    notifications = [
        m.text for m in driver.session.calls if isinstance(m, SendMessage) and m.chat_id == AUTHOR
    ]
    assert any("не прошло проверку" in text for text in notifications)
    assert all("не подходит формату" not in text for text in notifications)


async def test_pending_notifies_admins_and_auto_approval_stays_in_database(driver, service):
    await driver.callback(await driver.draft("когда отменяют пары"))
    worker = NotificationWorker(service, driver.bot)
    while await worker.process_once():
        pass
    notices = [
        m
        for m in driver.session.calls
        if isinstance(m, SendMessage) and m.text == "в предложке новое предложение ♡"
    ]
    assert {m.chat_id for m in notices} == {ADMIN, OTHER_ADMIN}
    assert all(m.reply_markup is not None for m in notices)
    service.settings.auto_approve_enabled = True
    await driver.callback(await driver.draft("люблю маму и папу", OTHER_AUTHOR), OTHER_AUTHOR)
    approved = (await service.mine(OTHER_AUTHOR))[0]
    assert approved.status == "approved"
    assert approved.published_at is None and approved.channel_message_id is None
    assert all(m.chat_id > 0 for m in driver.session.calls if isinstance(m, SendMessage))


async def test_about_privacy_and_notification_toggle(driver, service):
    await driver.message("/privacy")
    assert str(service.settings.retention_days) in driver.session.calls[-1].text
    await driver.callback("u:about")
    assert "Telegram ID" in driver.session.calls[-1].text
    await driver.callback("u:notify")
    assert await service.notifications_enabled(AUTHOR) is False
    await driver.callback("u:notify")
    assert await service.notifications_enabled(AUTHOR) is True


@pytest.mark.parametrize(
    "data",
    [
        "a:home",
        "a:stats",
        "a:settings",
        "a:q:pending:0",
        "a:q:approved:0",
        "a:ok:1",
        "a:no:1",
        "a:reject:1:spam",
        "a:card:1",
        "a:unknown",
    ],
)
async def test_every_admin_callback_requires_permission(driver, service, data):
    await driver.callback(data, OTHER_AUTHOR)
    assert isinstance(driver.session.calls[-1], AnswerCallbackQuery)
    assert "только модераторам" in driver.session.calls[-1].text
    assert not await service.mine(OTHER_AUTHOR)


async def test_own_history_and_forged_tokens(driver, service):
    first = await driver.draft("маму", AUTHOR)
    await driver.callback(first, OTHER_AUTHOR)
    assert not await service.mine(AUTHOR) and not await service.mine(OTHER_AUTHOR)
    await driver.callback(first, AUTHOR)
    second = await driver.draft("папу", OTHER_AUTHOR)
    await driver.callback(second, OTHER_AUTHOR)
    await driver.callback("u:mine", AUTHOR)
    history = driver.session.calls[-1].text
    assert "маму" in history and "папу" not in history
    await driver.callback("u:mine:100002", AUTHOR)
    assert isinstance(driver.session.calls[-1], AnswerCallbackQuery)


async def test_cancel_edit_expired_and_restart_drafts(driver, service):
    old = await driver.draft("люблю маму")
    await driver.callback(driver.button("u:edit:"))
    await driver.message("люблю папу")
    new = driver.button("u:send:")
    await driver.callback(old)
    assert not await service.mine(AUTHOR)
    await driver.callback(new)
    assert (await service.mine(AUTHOR))[0].normalized_text == "папу"
    button = await driver.draft("чай")
    await driver.callback("u:cancel")
    await driver.callback(button)
    assert len(await service.mine(AUTHOR)) == 1
    button = await driver.draft("университет")
    # Simulate restart: unconfirmed drafts disappear; committed records remain.
    await driver.dp.storage.close()
    await driver.callback(button)
    assert len(await service.mine(AUTHOR)) == 1


async def test_media_and_empty_content_do_not_submit(driver, service):
    await driver.callback("u:propose")
    await driver.message(None)
    assert "обычным текстом" in driver.session.calls[-1].text
    await driver.message("люблю")
    assert "продолжение" in driver.session.calls[-1].text
    await driver.message("подпишись https://spam.ru")
    assert "не прошло проверку" in driver.session.calls[-1].text
    assert not await service.mine(AUTHOR)


async def test_blocked_bot_does_not_rollback_review(driver, service):
    await driver.callback(await driver.draft())
    submission = (await service.mine(AUTHOR))[0]
    await driver.callback(f"a:ok:{submission.id}", ADMIN)
    driver.session.failure = lambda method: TelegramForbiddenError(
        method=method, message="bot was blocked"
    )
    worker = NotificationWorker(service, driver.bot)
    while await worker.process_once():
        pass
    assert (await service.mine(AUTHOR))[0].status == "approved"
    async with service.sessions() as session:
        user = await session.get(BotUser, AUTHOR)
        assert user.notifications_enabled is False
    # A fresh service instance picks up the committed decision.
    fresh = SubmissionService(service.sessions, service.settings)
    assert (await fresh.mine(AUTHOR))[0].status == "approved"


async def test_outbox_retries_transient_network_failure(driver, service):
    await driver.callback(await driver.draft())
    driver.session.failure = lambda method: TelegramNetworkError(
        method=method, message="temporary failure"
    )
    assert await NotificationWorker(service, driver.bot).process_once()
    async with service.sessions() as session:
        notice = await session.scalar(select(Notification).where(Notification.attempts == 1))
        assert notice.completed_at is None and notice.next_attempt_at > notice.created_at


async def test_delete_command_requires_current_confirmation(driver, service):
    await driver.callback(await driver.draft())
    await driver.message("/delete_my_data")
    token = driver.button("u:erase:")
    await driver.callback(token, OTHER_AUTHOR)
    assert len(await service.mine(AUTHOR)) == 1
    await driver.callback(token, AUTHOR)
    assert not await service.mine(AUTHOR)
    await driver.callback(token, AUTHOR)
    assert "не действует" in driver.session.calls[-1].text


async def test_incoming_groups_ignored_and_outgoing_channels_blocked(driver):
    await driver.message("/start", chat_id=-100123, chat_type="supergroup")
    assert not driver.session.calls
    await driver.callback("a:home", ADMIN, chat_id=-100123, chat_type="supergroup")
    assert isinstance(driver.session.calls[-1], AnswerCallbackQuery)
    for target in (-100123, "@some_channel", "123456"):
        before = len(driver.session.calls)
        with pytest.raises(PermissionError):
            await driver.bot.send_message(target, "must never be sent")
        assert len(driver.session.calls) == before
