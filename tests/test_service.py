import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import ADMIN, AUTHOR, OTHER_ADMIN, OTHER_AUTHOR
from sqlalchemy import func, select, update

from love_bot.models import BotUser, ModerationAudit, Notification, Submission
from love_bot.service import DuplicateSubmission, RateLimitExceeded, SubmissionError

pytestmark = pytest.mark.postgres


async def submit(service, text="тебя", user_id=AUTHOR, key=None):
    preview = service.preview(text)
    result, _ = await service.submit(user_id, text, preview.text, key or uuid4())
    return result


async def count(service, model):
    async with service.sessions() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_confirmation_and_ownership(service):
    with pytest.raises(SubmissionError):
        await service.submit(AUTHOR, "люблю тебя", "другой текст", uuid4())
    assert await count(service, Submission) == 0
    a = await submit(service)
    b = await submit(service, "нашу группу", OTHER_AUTHOR)
    assert [s.id for s in await service.mine(AUTHOR)] == [a.id]
    assert [s.id for s in await service.mine(OTHER_AUTHOR)] == [b.id]
    assert a.normalized_text == "тебя" and a.status == "pending"
    assert a.created_at.utcoffset() == timedelta(0)
    assert a.published_at is None and a.channel_message_id is None


async def test_request_idempotency_survives_concurrent_calls(service):
    key = uuid4()

    async def run():
        return await service.submit(AUTHOR, "люблю тебя", "тебя", key)

    results = await asyncio.gather(run(), run(), run())
    assert sum(created for _, created in results) == 1
    assert len({s.id for s, _ in results}) == 1
    assert await count(service, Submission) == 1
    assert await count(service, ModerationAudit) == 1
    async with service.sessions() as session:
        user = await session.get(BotUser, AUTHOR)
        assert user.submissions_count == 1
    with pytest.raises(PermissionError):
        await service.submit(OTHER_AUTHOR, "люблю тебя", "тебя", key)


async def test_identical_texts_and_rate_limit_are_atomic(service):
    await submit(service, "люблю тебя")
    with pytest.raises(DuplicateSubmission):
        await submit(service, "ТЕБЯ")
    results = await asyncio.gather(
        *(submit(service, text) for text in ["маму", "папу", "друзей", "чай"]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, Submission) for result in results) == 2
    assert sum(isinstance(result, RateLimitExceeded) for result in results) == 2
    assert len(await service.mine(AUTHOR)) == 3
    # This is a rolling one-hour window, not a lifetime cap.
    async with service.sessions.begin() as session:
        await session.execute(
            update(Submission).values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
    await submit(service, "университет")


async def test_duplicate_window_expires(service):
    await submit(service)
    async with service.sessions.begin() as session:
        await session.execute(
            update(Submission).values(created_at=datetime.now(UTC) - timedelta(hours=25))
        )
    await submit(service)
    assert await count(service, Submission) == 2


async def test_competing_moderators_have_one_winner(service):
    submission = await submit(service)
    results = await asyncio.gather(
        service.review(ADMIN, submission.id, "approved"),
        service.review(OTHER_ADMIN, submission.id, "rejected", "spam"),
    )
    assert sum(results) == 1
    stored = await service.get_for_admin(ADMIN, submission.id)
    assert stored.status in {"approved", "rejected"}
    assert stored.reviewed_by in {ADMIN, OTHER_ADMIN} and stored.reviewed_at
    assert not await service.review(ADMIN, submission.id, "approved")
    async with service.sessions() as session:
        audits = list(
            await session.scalars(
                select(ModerationAudit).where(ModerationAudit.action.in_(["approved", "rejected"]))
            )
        )
        assert len(audits) == 1
        assert audits[0].details["from"] == "pending"
        assert audits[0].details["to"] == stored.status
        author_notices = list(
            await session.scalars(select(Notification).where(Notification.target_id == AUTHOR))
        )
        assert len(author_notices) == 1


async def test_review_permissions_reasons_and_future_status(service):
    submission = await submit(service)
    with pytest.raises(PermissionError):
        await service.review(AUTHOR, submission.id, "approved")
    with pytest.raises(PermissionError):
        await service.queue(AUTHOR)
    with pytest.raises(PermissionError):
        await service.statistics(AUTHOR)
    with pytest.raises(ValueError):
        await service.review(ADMIN, submission.id, "published")
    with pytest.raises(ValueError):
        await service.review(ADMIN, submission.id, "rejected", "custom unchecked reason")
    assert await service.review(ADMIN, submission.id, "rejected", "personal")
    stored = await service.get_for_admin(ADMIN, submission.id)
    assert stored.rejection_reason == "личные данные"
    assert stored.moderation_decision == "manual_review"


async def test_auto_approval_does_not_publish_and_stats_are_real(service):
    service.settings.auto_approve_enabled = True
    approved = await submit(service, "люблю маму и папу")
    pending = await submit(service, "тебя")
    assert approved.status == "approved" and approved.reviewed_by is None
    assert approved.reviewed_at is not None
    assert approved.moderation_confidence is None
    assert pending.status == "pending"
    stats = await service.statistics(ADMIN)
    assert stats["users"] == 1 and stats["total"] == 2 and stats["day"] == 2
    assert stats["approved"] == 1 and stats["pending"] == 1
    assert stats["automatic_share"] == 0.5
    assert stats["authors_7"] == stats["authors_30"] == 1
    assert approved.channel_message_id is None


async def test_privacy_deletes_approved_pending_audit_and_notifications(service):
    approved = await submit(service)
    await service.review(ADMIN, approved.id, "approved")
    await submit(service, "маму")
    other = await submit(service, "чай", OTHER_AUTHOR)
    await service.delete_user_data(AUTHOR)
    assert not await service.mine(AUTHOR)
    assert [s.id for s in await service.mine(OTHER_AUTHOR)] == [other.id]
    assert not await service.review(ADMIN, approved.id, "rejected", "spam")
    async with service.sessions() as session:
        assert await session.get(BotUser, AUTHOR) is None
        assert await session.scalar(select(func.count()).select_from(ModerationAudit)) == 1
        assert await session.scalar(select(func.count()).select_from(Notification)) == len(
            service.settings.admin_ids
        )


async def test_automatic_retention_and_notification_opt_out(service):
    old = await submit(service)
    recent = await submit(service, "чай", OTHER_AUTHOR)
    assert await service.toggle_notifications(OTHER_AUTHOR) is False
    await service.review(ADMIN, recent.id, "approved")
    async with service.sessions.begin() as session:
        await session.execute(
            update(Submission)
            .where(Submission.id == old.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=181))
        )
        await session.execute(
            update(Submission)
            .where(Submission.id == recent.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=8))
        )
        await session.execute(
            update(BotUser)
            .where(BotUser.telegram_id == AUTHOR)
            .values(last_seen_at=datetime.now(UTC) - timedelta(days=181))
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Notification)
                .where(Notification.target_id == OTHER_AUTHOR)
            )
            == 0
        )
    await service.cleanup()
    assert not await service.mine(AUTHOR)
    remaining = await service.mine(OTHER_AUTHOR)
    assert len(remaining) == 1 and remaining[0].original_text is None
    async with service.sessions() as session:
        assert await session.get(BotUser, AUTHOR) is None
    assert await service.toggle_notifications(OTHER_AUTHOR) is True
