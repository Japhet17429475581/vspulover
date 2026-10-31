import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from conftest import ADMIN, AUTHOR, OTHER_ADMIN, OTHER_AUTHOR
from pydantic import ValidationError
from sqlalchemy import func, select, update

from love_bot.config import Settings
from love_bot.models import ModerationAudit, PublicationJob, PublicationState, Submission
from love_bot.notifications import NotificationWorker
from love_bot.publication import PublicationService, PublicationWorker

CHANNEL = -1001234567890


def enable(service):
    service.settings.channel_id = CHANNEL
    service.settings.publication_enabled = True
    service.settings.publication_interval_seconds = 60


async def approved(service, text="люблю тебя", user_id=AUTHOR):
    preview = service.preview(text)
    submission, _ = await service.submit(user_id, text, preview.text, uuid4())
    if submission.status == "pending":
        await service.review(ADMIN, submission.id, "approved")
    return submission


def channel_calls(driver):
    return [m for m in driver.session.calls if isinstance(m, SendMessage) and m.chat_id < 0]


async def make_due(service):
    async with service.sessions.begin() as session:
        past = datetime.now(UTC) - timedelta(seconds=1)
        await session.execute(update(PublicationState).values(next_send_at=past))
        await session.execute(update(PublicationJob).values(next_attempt_at=past))


@pytest.mark.parametrize("channel", ["@some_channel", 123, 0])
def test_invalid_channel_settings(settings, channel):
    values = settings.model_dump()
    values.update(channel_id=channel, publication_enabled=True)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


def test_publication_requires_channel(settings):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **(settings.model_dump() | {"publication_enabled": True}))
    configured = Settings(
        _env_file=None,
        **(settings.model_dump() | {"channel_id": CHANNEL, "publication_enabled": True}),
    )
    assert configured.channel_id == CHANNEL


@pytest.mark.postgres
async def test_full_path_publishes_only_confirmed_text_once(driver, service, caplog):
    enable(service)
    await driver.message("/start")
    button = await driver.draft("люблю Маму и папу ❤️")
    worker = PublicationWorker(service, driver.bot)
    assert not await worker.process_once()  # Drafts cannot enter the publisher.
    await driver.callback(button)
    submission = (await service.mine(AUTHOR))[0]
    assert not await worker.process_once()  # Pending is never publishable.
    await asyncio.gather(
        driver.callback(f"a:ok:{submission.id}", ADMIN),
        driver.callback(f"a:ok:{submission.id}", OTHER_ADMIN),
    )
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "queued"
    assert "в очереди" in driver.session.calls[-1].text
    results = await asyncio.gather(
        worker.process_once(), PublicationWorker(service, driver.bot).process_once()
    )
    assert sum(results) == 1
    sent = channel_calls(driver)
    assert len(sent) == 1 and sent[0].chat_id == CHANNEL and sent[0].text == "Маму и папу ❤️"
    assert sent[0].parse_mode is None and sent[0].reply_markup is None
    assert str(AUTHOR) not in sent[0].text
    stored = (await service.mine(AUTHOR))[0]
    assert stored.status == "published" and stored.published_at and stored.channel_message_id
    assert not await worker.process_once()
    assert not await worker.publication.enqueue(ADMIN, submission.id)
    async with service.sessions() as session:
        audits = list(
            await session.scalars(
                select(ModerationAudit).where(ModerationAudit.action == "published")
            )
        )
        assert len(audits) == 1 and audits[0].details["message_id"] == stored.channel_message_id
    stats = await service.statistics(ADMIN)
    assert stats["published"] == 1
    await driver.callback("u:mine")
    assert "опубликовано" in driver.session.calls[-1].text
    notices = NotificationWorker(service, driver.bot)
    while await notices.process_once():
        pass
    assert any(
        isinstance(m, SendMessage) and m.chat_id == AUTHOR and "одобрено" in m.text
        for m in driver.session.calls
    )
    assert not [r for r in caplog.records if r.levelno >= 40]


@pytest.mark.postgres
async def test_auto_confession_uses_same_publication_queue(driver, service):
    enable(service)
    service.settings.auto_approve_enabled = True
    await driver.callback(await driver.draft("люблю Машу из нашей группы ❤️"))
    submission = (await service.mine(AUTHOR))[0]
    assert (await service.mine(AUTHOR))[0].status == "approved"
    assert submission.reviewed_by is None
    worker = PublicationWorker(service, driver.bot)
    assert await worker.process_once()
    assert (await service.mine(AUTHOR))[0].status == "published"
    assert (await worker.publication.get(ADMIN, submission.id)).status == "published"
    assert channel_calls(driver)[0].text == "Машу из нашей группы ❤️"


@pytest.mark.postgres
async def test_off_mode_and_manual_old_approved_queue(driver, service):
    submission = await approved(service)
    worker = PublicationWorker(service, driver.bot)
    assert not await worker.process_once()
    assert await worker.publication.get(ADMIN, submission.id) is None
    enable(service)
    assert not await worker.process_once()  # Enabling does not post the entire old backlog.
    await driver.callback(f"a:publish:{submission.id}", AUTHOR)
    assert not channel_calls(driver)
    await driver.callback(f"a:publish:{submission.id}", ADMIN)
    await driver.callback(f"a:publish:{submission.id}", ADMIN)
    assert await worker.process_once()
    assert len(channel_calls(driver)) == 1


@pytest.mark.postgres
async def test_pause_is_shared_and_persists_across_workers(driver, service):
    enable(service)
    await approved(service, "маму", AUTHOR)
    await approved(service, "папу", OTHER_AUTHOR)
    assert await PublicationWorker(service, driver.bot).process_once()
    assert not await PublicationWorker(service, driver.bot).process_once()
    assert len(channel_calls(driver)) == 1
    await make_due(service)
    assert await PublicationWorker(service, driver.bot).process_once()
    assert len(channel_calls(driver)) == 2


@pytest.mark.postgres
async def test_retry_after_is_the_only_automatic_resend(driver, service):
    enable(service)
    submission = await approved(service)
    driver.session.failure = lambda method: TelegramRetryAfter(
        method=method, message="flood", retry_after=30
    )
    worker = PublicationWorker(service, driver.bot)
    assert await worker.process_once()
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "queued" and job.attempts == 1
    assert not await worker.process_once()
    await make_due(service)
    driver.session.failure = None
    assert await worker.process_once()
    assert (await service.mine(AUTHOR))[0].status == "published"


@pytest.mark.postgres
@pytest.mark.parametrize("error", [TelegramBadRequest, TelegramForbiddenError])
async def test_definite_failure_needs_authorized_manual_retry(driver, service, error):
    enable(service)
    submission = await approved(service)
    driver.session.failure = lambda method: error(method=method, message="no permission")
    worker = PublicationWorker(service, driver.bot)
    assert await worker.process_once()
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "failed" and job.attempts == 1
    assert (await service.mine(AUTHOR))[0].status == "approved"
    assert not await worker.process_once()
    with pytest.raises(PermissionError):
        await worker.publication.retry(AUTHOR, submission.id, 1)
    await driver.callback("a:publications:0", ADMIN)
    assert "ошибка" in driver.session.calls[-1].text
    await driver.callback(f"a:pub_retry:{submission.id}:1", ADMIN)
    assert "дубль" in driver.session.calls[-1].text
    await driver.callback(driver.button("a:pub_retry_confirm:"), ADMIN)
    assert not await worker.publication.retry(ADMIN, submission.id, 1)
    await make_due(service)
    driver.session.failure = None
    assert await worker.process_once()
    assert (await service.mine(AUTHOR))[0].status == "published"


@pytest.mark.postgres
@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramServerError, TimeoutError])
async def test_unknown_outcome_is_not_retried_and_can_be_reconciled(driver, service, error):
    enable(service)
    submission = await approved(service)
    driver.session.failure = (
        (lambda method: TimeoutError())
        if error is TimeoutError
        else (lambda method: error(method=method, message="unknown result"))
    )
    worker = PublicationWorker(service, driver.bot)
    assert await worker.process_once()
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "uncertain"
    await make_due(service)
    assert not await worker.process_once()
    assert len(channel_calls(driver)) == 1
    with pytest.raises(PermissionError):
        await worker.publication.mark_published(AUTHOR, submission.id, 321)
    driver.session.failure = None
    await driver.message(f"/published {submission.id} 321", ADMIN)
    stored = (await service.mine(AUTHOR))[0]
    assert stored.status == "published" and stored.channel_message_id == 321
    assert not await worker.publication.retry(ADMIN, submission.id, job.attempts)
    assert not await worker.process_once()
    assert len(channel_calls(driver)) == 1


@pytest.mark.postgres
async def test_crash_after_claim_recovers_to_uncertain(driver, service):
    enable(service)
    submission = await approved(service)
    worker = PublicationWorker(service, driver.bot)
    assert await worker.claim() == submission.id
    assert (await worker.publication.get(ADMIN, submission.id)).status == "sending"
    fresh = PublicationService(service)
    await fresh.recover_interrupted()
    assert (await fresh.get(ADMIN, submission.id)).status == "uncertain"
    assert not await PublicationWorker(service, driver.bot).process_once()
    assert not channel_calls(driver)


@pytest.mark.postgres
async def test_commit_failure_after_send_does_not_resend(driver, service, monkeypatch):
    enable(service)
    submission = await approved(service)
    worker = PublicationWorker(service, driver.bot)

    def fail_commit(*args, **kwargs):
        raise RuntimeError("simulated persistence failure after Telegram accepted the post")

    monkeypatch.setattr(worker.publication, "complete", fail_commit)
    assert await worker.process_once()
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "uncertain" and job.error_code == "PublicationCommitFailed"
    await make_due(service)
    assert not await worker.process_once()
    assert len(channel_calls(driver)) == 1


@pytest.mark.postgres
async def test_deleting_data_cancels_pending_jobs_and_keeps_existing_posts(driver, service):
    enable(service)
    unpublished = await approved(service)
    publication = PublicationService(service)
    await service.delete_user_data(AUTHOR)
    assert await publication.get(ADMIN, unpublished.id) is None
    assert not await PublicationWorker(service, driver.bot).process_once()
    published = await approved(service, "маму", OTHER_AUTHOR)
    assert await PublicationWorker(service, driver.bot).process_once()
    await service.delete_user_data(OTHER_AUTHOR)
    assert await publication.get(ADMIN, published.id) is None
    assert len(channel_calls(driver)) == 1  # Never calls Telegram deleteMessage.


@pytest.mark.postgres
async def test_channel_switch_requires_review_and_other_destinations_stay_blocked(driver, service):
    enable(service)
    submission = await approved(service)
    service.settings.channel_id = CHANNEL - 1
    worker = PublicationWorker(service, driver.bot)
    assert await worker.process_once()
    assert not channel_calls(driver)
    job = await worker.publication.get(ADMIN, submission.id)
    assert job.status == "failed" and job.error_code == "ChannelChanged"
    with pytest.raises(PermissionError):
        await driver.bot.send_message(CHANNEL, "old channel")
    with pytest.raises(PermissionError):
        await driver.bot.send_message("@some_channel", "unexpected destination")


@pytest.mark.postgres
async def test_channel_validation_requires_channel_admin_rights(driver, service, monkeypatch):
    enable(service)

    async def chat(*args, **kwargs):
        return SimpleNamespace(type="channel")

    async def member(*args, **kwargs):
        return SimpleNamespace(can_post_messages=True)

    monkeypatch.setattr(driver.bot, "get_chat", chat)
    monkeypatch.setattr(driver.bot, "get_chat_member", member)
    worker = PublicationWorker(service, driver.bot)
    await worker.validate_channel()

    async def forbidden(*args, **kwargs):
        return SimpleNamespace(can_post_messages=False)

    monkeypatch.setattr(driver.bot, "get_chat_member", forbidden)
    with pytest.raises(RuntimeError):
        await worker.validate_channel()


@pytest.mark.postgres
async def test_queue_unique_and_no_jobs_for_rejected_content(driver, service):
    enable(service)
    await driver.callback(await driver.draft())
    submission = (await service.mine(AUTHOR))[0]
    await service.review(ADMIN, submission.id, "rejected", "spam")
    assert not await PublicationService(service).enqueue(ADMIN, submission.id)
    async with service.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PublicationJob)) == 0
        assert (await session.get(Submission, submission.id)).status == "rejected"
