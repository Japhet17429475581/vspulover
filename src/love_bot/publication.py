"""Durable channel publishing. Ambiguous Telegram outcomes require human reconciliation."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions
from sqlalchemy import select, text, update

from love_bot.diagnostics import StartupFailure, startup_call
from love_bot.models import BotUser, ModerationAudit, PublicationJob, PublicationState, Submission
from love_bot.service import SubmissionError

logger = logging.getLogger(__name__)


class PublicationService:
    def __init__(self, service):
        self.service = service
        self.sessions = service.sessions
        self.settings = service.settings

    async def lock_owner(self, session, submission_id):
        owner = await session.scalar(
            select(Submission.user_id).where(Submission.id == submission_id)
        )
        if owner is None:
            return None
        return await session.scalar(
            select(BotUser).where(BotUser.telegram_id == owner).with_for_update()
        )

    async def enqueue(self, admin_id: int, submission_id: int) -> bool:
        self.service.require_admin(admin_id)
        if not self.settings.publication_enabled:
            raise SubmissionError("сначала включи публикацию и задай CHANNEL_ID в окружении")
        async with self.sessions.begin() as session:
            if await self.lock_owner(session, submission_id) is None:
                return False
            submission = await session.get(Submission, submission_id)
            job = await session.get(PublicationJob, submission_id)
            if submission.status != "approved" or job is not None:
                return False
            self.service.enqueue_publication(session, submission_id, admin_id)
            return True

    async def get(self, admin_id, submission_id):
        self.service.require_admin(admin_id)
        async with self.sessions() as session:
            return await session.get(PublicationJob, submission_id)

    async def next(self, admin_id, after_id=0):
        self.service.require_admin(admin_id)
        async with self.sessions() as session:
            return (
                await session.execute(
                    select(Submission, PublicationJob)
                    .join(PublicationJob)
                    .where(PublicationJob.status != "published", Submission.id > after_id)
                    .order_by(Submission.id)
                    .limit(1)
                )
            ).first()

    async def retry(self, admin_id, submission_id, expected_attempts) -> bool:
        self.service.require_admin(admin_id)
        if not self.settings.publication_enabled:
            raise SubmissionError("публикация выключена в настройках окружения")
        async with self.sessions.begin() as session:
            if await self.lock_owner(session, submission_id) is None:
                return False
            job = await session.get(PublicationJob, submission_id, with_for_update=True)
            submission = await session.get(Submission, submission_id)
            if (
                job is None
                or job.status not in {"failed", "uncertain"}
                or job.attempts != expected_attempts
                or submission.status != "approved"
            ):
                return False
            previous = job.status
            job.status = "queued"
            job.channel_id = self.settings.channel_id
            job.error_code = None
            job.next_attempt_at = datetime.now(UTC)
            job.started_at = None
            job.finished_at = None
            session.add(
                ModerationAudit(
                    submission_id=submission_id,
                    action="publication_retry",
                    moderator_id=admin_id,
                    details={
                        "from": previous,
                        "to": "queued",
                        "attempts": expected_attempts,
                        "channel_id": job.channel_id,
                    },
                )
            )
            return True

    async def mark_published(self, admin_id, submission_id, message_id) -> bool:
        self.service.require_admin(admin_id)
        if not 0 < message_id < 2**63:
            raise SubmissionError("укажи положительный message_id поста в канале")
        async with self.sessions.begin() as session:
            if await self.lock_owner(session, submission_id) is None:
                return False
            job = await session.get(PublicationJob, submission_id, with_for_update=True)
            submission = await session.get(Submission, submission_id)
            if job is None or job.status != "uncertain" or submission.status != "approved":
                return False
            self.complete(session, submission, job, message_id, admin_id)
            return True

    @staticmethod
    def complete(session, submission, job, message_id, moderator_id=None):
        now = datetime.now(UTC)
        submission.status = "published"
        submission.published_at = now
        submission.channel_message_id = message_id
        submission.updated_at = now
        job.status = "published"
        job.finished_at = now
        job.error_code = None
        session.add(
            ModerationAudit(
                submission_id=submission.id,
                action="published",
                moderator_id=moderator_id,
                details={
                    "from": "approved",
                    "to": "published",
                    "channel_id": job.channel_id,
                    "message_id": message_id,
                    "reconciled": moderator_id is not None,
                },
            )
        )

    async def recover_interrupted(self):
        # Called only at startup, after the application's singleton lock and before polling.
        async with self.sessions.begin() as session:
            ids = list(
                await session.scalars(
                    update(PublicationJob)
                    .where(PublicationJob.status == "sending")
                    .values(status="uncertain", error_code="InterruptedPublication")
                    .returning(PublicationJob.submission_id)
                )
            )
            for submission_id in ids:
                session.add(
                    ModerationAudit(
                        submission_id=submission_id,
                        action="publication_uncertain",
                        details={"error": "InterruptedPublication"},
                    )
                )


class PublicationWorker:
    def __init__(self, service, bot):
        self.publication = PublicationService(service)
        self.settings = service.settings
        self.sessions = service.sessions
        self.bot = bot

    async def validate_channel(self):
        if not self.settings.publication_enabled:
            return
        chat = await startup_call(
            "get_publication_channel", self.bot.get_chat(self.settings.channel_id)
        )
        member = await startup_call(
            "check_publication_permissions",
            self.bot.get_chat_member(self.settings.channel_id, self.bot.id),
        )
        if chat.type != ChatType.CHANNEL:
            logger.error(
                "publication_target_must_be_channel",
                extra={
                    "stage": "get_publication_channel",
                    "hint": "CHANNEL_ID указывает на другой тип чата. Укажи ID канала, а не группы.",
                },
            )
            raise StartupFailure("Publication target must be a channel")
        if not getattr(member, "can_post_messages", False):
            logger.error(
                "publication_admin_post_permission_required",
                extra={
                    "stage": "check_publication_permissions",
                    "hint": "Назначь бота администратором канала с правом публикации сообщений.",
                },
            )
            raise StartupFailure("Bot must be a channel administrator with can_post_messages")

    async def claim(self):
        if not self.settings.publication_enabled:
            return None
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            submission_id = await session.scalar(
                select(PublicationJob.submission_id)
                .where(
                    PublicationJob.status == "queued",
                    PublicationJob.next_attempt_at <= now,
                )
                .order_by(PublicationJob.next_attempt_at, PublicationJob.submission_id)
                .limit(1)
            )
            if (
                submission_id is None
                or await self.publication.lock_owner(session, submission_id) is None
            ):
                return None
            # Consistent lock order: owner -> global publisher state -> job.
            gate = await session.get(PublicationState, 1, with_for_update=True)
            if gate.next_send_at > datetime.now(UTC):
                return None
            job = await session.get(PublicationJob, submission_id, with_for_update=True)
            submission = await session.get(Submission, submission_id)
            if job is None or job.status != "queued" or job.next_attempt_at > datetime.now(UTC):
                return None
            job.attempts += 1
            if job.channel_id != self.settings.channel_id or submission.status != "approved":
                job.status = "failed"
                job.error_code = (
                    "ChannelChanged"
                    if job.channel_id != self.settings.channel_id
                    else "NotApproved"
                )
                session.add(
                    ModerationAudit(
                        submission_id=submission_id,
                        action="publication_failed",
                        details={"error": job.error_code},
                    )
                )
                return submission_id
            job.status = "sending"
            job.started_at = datetime.now(UTC)
            # Commit intent before contacting Telegram. A crash never triggers a blind resend.
            gate.next_send_at = job.started_at + timedelta(
                seconds=self.settings.publication_interval_seconds
            )
            session.add(
                ModerationAudit(
                    submission_id=submission_id,
                    action="publication_started",
                    details={"attempt": job.attempts, "channel_id": job.channel_id},
                )
            )
            return submission_id

    async def deliver(self, submission_id):
        async with self.sessions.begin() as session:
            if await self.publication.lock_owner(session, submission_id) is None:
                return
            gate = await session.get(PublicationState, 1, with_for_update=True)
            job = await session.get(PublicationJob, submission_id, with_for_update=True)
            submission = await session.get(Submission, submission_id)
            if job is None or job.status != "sending" or submission.status != "approved":
                return
            if job.channel_id != self.settings.channel_id or not self.settings.publication_enabled:
                job.status, job.error_code = "failed", "ChannelChanged"
                return
            try:
                message = await self.bot.send_message(
                    job.channel_id,
                    submission.normalized_text,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    request_timeout=15,
                )
                if message.chat.id != job.channel_id or message.message_id <= 0:
                    raise RuntimeError("UnexpectedPublicationResponse")
            except TelegramRetryAfter as exc:
                job.status = "queued"
                job.error_code = "TelegramRetryAfter"
                job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=exc.retry_after + 1)
                gate.next_send_at = max(gate.next_send_at, job.next_attempt_at)
                session.add(
                    ModerationAudit(
                        submission_id=submission_id,
                        action="publication_rate_limited",
                        details={"retry_after": exc.retry_after},
                    )
                )
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                job.status = "failed"
                job.error_code = type(exc).__name__
                session.add(
                    ModerationAudit(
                        submission_id=submission_id,
                        action="publication_failed",
                        details={"error": job.error_code},
                    )
                )
                logger.warning("publication_failed", extra={"error_type": job.error_code})
            except Exception as exc:
                job.status = "uncertain"
                job.error_code = type(exc).__name__
                session.add(
                    ModerationAudit(
                        submission_id=submission_id,
                        action="publication_uncertain",
                        details={"error": job.error_code},
                    )
                )
                logger.warning("publication_uncertain", extra={"error_type": job.error_code})
            else:
                self.publication.complete(session, submission, job, message.message_id)
            gate.next_send_at = max(
                gate.next_send_at,
                datetime.now(UTC) + timedelta(seconds=self.settings.publication_interval_seconds),
            )

    async def process_once(self) -> bool:
        # Serialize the complete claim -> network -> commit cycle even if two workers
        # are accidentally started. This also preserves spacing between actual sends.
        async with self.sessions.begin() as lock_session:
            if not await lock_session.scalar(text("SELECT pg_try_advisory_xact_lock(762950132)")):
                return False
            return await self._process_locked()

    async def _process_locked(self) -> bool:
        submission_id = await self.claim()
        if submission_id is None:
            return False
        try:
            await self.deliver(submission_id)
        except Exception as exc:
            # Includes DB failure after Telegram succeeded. The committed 'sending' intent
            # must survive and be reconciled, never automatically submitted a second time.
            logger.error("publication_commit_failed", extra={"error_type": type(exc).__name__})
            async with self.sessions.begin() as session:
                if await self.publication.lock_owner(session, submission_id) is not None:
                    result = await session.execute(
                        update(PublicationJob)
                        .where(
                            PublicationJob.submission_id == submission_id,
                            PublicationJob.status == "sending",
                        )
                        .values(status="uncertain", error_code="PublicationCommitFailed")
                    )
                    if result.rowcount:
                        session.add(
                            ModerationAudit(
                                submission_id=submission_id,
                                action="publication_uncertain",
                                details={"error": "PublicationCommitFailed"},
                            )
                        )
        return True

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:
                await self.process_once()
            except Exception as exc:
                logger.error("publisher_failed", extra={"error_type": type(exc).__name__})
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
