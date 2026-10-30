import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from love_bot import ui
from love_bot.models import BotUser, Notification, Submission

logger = logging.getLogger(__name__)


class NotificationWorker:
    def __init__(self, service, bot):
        self.service = service
        self.bot = bot

    async def process_once(self) -> bool:
        now = datetime.now(UTC)
        async with self.service.sessions.begin() as session:
            candidate = (
                await session.execute(
                    select(Notification.id, Submission.user_id)
                    .join(Submission, Notification.submission_id == Submission.id)
                    .where(Notification.completed_at.is_(None), Notification.next_attempt_at <= now)
                    .order_by(Notification.id)
                    .limit(1)
                )
            ).first()
            if candidate is None:
                return False
            user = await session.scalar(
                select(BotUser).where(BotUser.telegram_id == candidate.user_id).with_for_update()
            )
            notice = await session.scalar(
                select(Notification)
                .where(Notification.id == candidate.id)
                .with_for_update(skip_locked=True)
            )
            if notice is None or notice.completed_at is not None:
                return True
            if notice.next_attempt_at > datetime.now(UTC):
                return True
            submission = await session.get(Submission, notice.submission_id)
            if user is None or submission is None:
                return True
            is_admin = notice.kind == "new_submission"
            allowed = (
                notice.target_id in self.service.settings.admin_ids
                and submission.status == "pending"
                if is_admin
                else user.notifications_enabled and submission.status == notice.kind
            )
            if not allowed:
                notice.completed_at = now
                return True
            text = (
                "в предложке новое предложение ♡"
                if is_admin
                else ui.APPROVED
                if notice.kind == "approved"
                else ui.REJECTED
            )
            notice.attempts += 1
            try:
                # Bounded network call. The DB transaction was committed by the reviewer earlier.
                await self.bot.send_message(
                    notice.target_id,
                    text,
                    reply_markup=ui.admin_home() if is_admin else None,
                    request_timeout=10,
                )
            except TelegramForbiddenError:
                notice.completed_at = now
                if not is_admin:
                    user.notifications_enabled = False
                logger.info("notification_forbidden")
            except TelegramBadRequest:
                notice.completed_at = now
                logger.warning("notification_invalid_recipient")
            except Exception as exc:
                delay = (
                    exc.retry_after
                    if isinstance(exc, TelegramRetryAfter)
                    else min(3600, 2 ** min(notice.attempts, 11))
                )
                notice.next_attempt_at = now + timedelta(seconds=delay + 1)
                if notice.attempts >= 8:
                    notice.completed_at = now
                logger.warning("notification_retry", extra={"error_type": type(exc).__name__})
            else:
                notice.completed_at = datetime.now(UTC)
            return True

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                has_work = await self.process_once()
            except Exception as exc:
                logger.error("outbox_failed", extra={"error_type": type(exc).__name__})
                has_work = False
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1 if has_work else 2)
            except TimeoutError:
                pass
