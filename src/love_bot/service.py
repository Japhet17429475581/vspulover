from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import and_, delete, distinct, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from love_bot.config import Settings
from love_bot.models import BotUser, ModerationAudit, Notification, Submission
from love_bot.moderation import (
    ModerationClassifier,
    ModerationResult,
    RuleBasedClassifier,
    apply_policy,
)
from love_bot.text_normalizer import fingerprint, prepare_text

REJECTION_REASONS = {
    "spam": "спам",
    "personal": "личные данные",
    "abuse": "оскорбление или травля",
    "format": "не подходит формату",
    "other": "другое",
}


class SubmissionError(ValueError):
    pass


class DuplicateSubmission(SubmissionError):
    def __init__(self):
        super().__init__("ты уже отправлял это предложение недавно ♡")


class RateLimitExceeded(SubmissionError):
    def __init__(self):
        super().__init__("пока достаточно ♡ попробуй отправить ещё немного позже")


class ContentRejected(SubmissionError):
    def __init__(self):
        super().__init__("это предложение не прошло проверку. попробуй написать другое ♡")


@dataclass(frozen=True)
class Preview:
    text: str
    moderation: ModerationResult


class SubmissionService:
    def __init__(
        self, sessions, settings: Settings, classifier: ModerationClassifier | None = None
    ):
        self.sessions = sessions
        self.settings = settings
        self.classifier = classifier or RuleBasedClassifier()

    def require_admin(self, admin_id: int) -> None:
        if admin_id not in self.settings.admin_ids:
            raise PermissionError("admin required")

    def preview(self, original: str) -> Preview:
        text = prepare_text(original, self.settings.max_text_length)
        result = apply_policy(
            self.classifier.classify(text, original),
            self.settings.auto_approve_enabled,
            self.settings.auto_approve_confidence,
        )
        if result.decision == "rejected":
            raise ContentRejected()
        return Preview(text, result)

    async def _lock_user(self, session, user_id: int) -> BotUser:
        if user_id <= 0:
            raise ValueError("private user ID required")
        await session.execute(insert(BotUser).values(telegram_id=user_id).on_conflict_do_nothing())
        # Serializes the rate limit, duplicate check, counter and privacy deletion per user.
        user = await session.scalar(
            select(BotUser).where(BotUser.telegram_id == user_id).with_for_update()
        )
        user.last_seen_at = datetime.now(UTC)
        return user

    async def touch_user(self, user_id: int) -> None:
        async with self.sessions.begin() as session:
            await self._lock_user(session, user_id)

    async def submit(
        self, user_id: int, original: str, confirmed_text: str, request_key: UUID
    ) -> tuple[Submission, bool]:
        preview = self.preview(original)
        if preview.text != confirmed_text:
            raise SubmissionError("текст изменился — проверь его ещё раз ♡")
        now = datetime.now(UTC)
        digest = fingerprint(preview.text)
        async with self.sessions.begin() as session:
            user = await self._lock_user(session, user_id)
            existing = await session.scalar(
                select(Submission).where(Submission.request_key == request_key)
            )
            if existing is not None:
                if existing.user_id != user_id:
                    raise PermissionError("request belongs to another user")
                return existing, False
            duplicate = await session.scalar(
                select(Submission.id)
                .where(
                    Submission.user_id == user_id,
                    Submission.text_fingerprint == digest,
                    Submission.created_at
                    >= now - timedelta(hours=self.settings.duplicate_window_hours),
                )
                .limit(1)
            )
            if duplicate is not None:
                raise DuplicateSubmission()
            count = await session.scalar(
                select(func.count())
                .select_from(Submission)
                .where(
                    Submission.user_id == user_id,
                    Submission.created_at >= now - timedelta(hours=1),
                )
            )
            if count >= self.settings.max_submissions_per_hour:
                raise RateLimitExceeded()
            result = preview.moderation
            approved = result.decision == "auto_approved"
            submission = Submission(
                user_id=user_id,
                request_key=request_key,
                original_text=original,
                normalized_text=preview.text,
                text_fingerprint=digest,
                status="approved" if approved else "pending",
                moderation_decision=result.decision,
                moderation_confidence=result.confidence,
                moderation_flags=list(result.flags),
                moderation_reasons=list(result.reasons),
                reviewed_at=now if approved else None,
            )
            session.add(submission)
            await session.flush()
            user.submissions_count += 1
            session.add(
                ModerationAudit(
                    submission_id=submission.id,
                    action="auto_approved" if approved else "submitted",
                    details={
                        "from": None,
                        "to": submission.status,
                        "classifier": type(self.classifier).__name__,
                    },
                )
            )
            if approved and user.notifications_enabled:
                session.add(
                    Notification(submission_id=submission.id, target_id=user_id, kind="approved")
                )
            if not approved:
                for admin_id in self.settings.admin_ids:
                    session.add(
                        Notification(
                            submission_id=submission.id, target_id=admin_id, kind="new_submission"
                        )
                    )
            return submission, True

    async def mine(self, user_id: int) -> list[Submission]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(Submission)
                    .where(Submission.user_id == user_id)
                    .order_by(Submission.created_at.desc(), Submission.id.desc())
                    .limit(10)
                )
            )

    async def queue(self, admin_id: int, status: str = "pending", after_id: int = 0):
        self.require_admin(admin_id)
        if status not in {"pending", "approved"}:
            raise ValueError("unsupported queue")
        async with self.sessions() as session:
            return await session.scalar(
                select(Submission)
                .where(Submission.status == status, Submission.id > after_id)
                .order_by(Submission.id)
                .limit(1)
            )

    async def get_for_admin(self, admin_id: int, submission_id: int):
        self.require_admin(admin_id)
        async with self.sessions() as session:
            return await session.get(Submission, submission_id)

    async def review(
        self, admin_id: int, submission_id: int, action: str, reason: str | None = None
    ) -> bool:
        self.require_admin(admin_id)
        if action not in {"approved", "rejected"}:
            raise ValueError("invalid review action")
        if action == "rejected" and reason not in REJECTION_REASONS:
            raise ValueError("rejection reason required")
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            # Keep the same user -> submission lock order as submit/delete/outbox.
            owner = await session.scalar(
                select(Submission.user_id).where(Submission.id == submission_id)
            )
            if owner is None:
                return False
            user = await session.scalar(
                select(BotUser).where(BotUser.telegram_id == owner).with_for_update()
            )
            if user is None:
                return False
            result = await session.execute(
                update(Submission)
                .where(Submission.id == submission_id, Submission.status == "pending")
                .values(
                    status=action,
                    reviewed_at=now,
                    reviewed_by=admin_id,
                    updated_at=now,
                    rejection_reason=REJECTION_REASONS[reason] if action == "rejected" else None,
                )
                .returning(Submission.id)
            )
            if result.scalar_one_or_none() is None:
                return False
            session.add(
                ModerationAudit(
                    submission_id=submission_id,
                    action=action,
                    moderator_id=admin_id,
                    details={"from": "pending", "to": action, "reason": reason},
                )
            )
            if user.notifications_enabled:
                session.add(Notification(submission_id=submission_id, target_id=owner, kind=action))
            return True

    async def statistics(self, admin_id: int) -> dict:
        self.require_admin(admin_id)
        now = datetime.now(UTC)
        async with self.sessions() as session:
            row = (
                (
                    await session.execute(
                        select(
                            func.count().label("total"),
                            func.count()
                            .filter(Submission.created_at >= now - timedelta(days=1))
                            .label("day"),
                            func.count().filter(Submission.status == "pending").label("pending"),
                            func.count().filter(Submission.status == "approved").label("approved"),
                            func.count().filter(Submission.status == "rejected").label("rejected"),
                            func.count()
                            .filter(
                                Submission.moderation_decision.in_(["auto_approved", "rejected"])
                            )
                            .label("automatic"),
                            func.count(distinct(Submission.user_id))
                            .filter(Submission.created_at >= now - timedelta(days=7))
                            .label("authors_7"),
                            func.count(distinct(Submission.user_id))
                            .filter(Submission.created_at >= now - timedelta(days=30))
                            .label("authors_30"),
                        )
                    )
                )
                .mappings()
                .one()
            )
            stats = dict(row)
            stats["users"] = await session.scalar(select(func.count()).select_from(BotUser))
            # Calendar day in the configured display timezone, separate from rolling 24h.
            stats["today"] = await session.scalar(
                select(func.count())
                .select_from(Submission)
                .where(
                    func.date(func.timezone(self.settings.display_timezone, Submission.created_at))
                    == now.astimezone(ZoneInfo(self.settings.display_timezone)).date()
                )
            )
            stats["automatic_share"] = stats["automatic"] / stats["total"] if stats["total"] else 0
            return stats

    async def notifications_enabled(self, user_id: int) -> bool:
        async with self.sessions() as session:
            enabled = await session.scalar(
                select(BotUser.notifications_enabled).where(BotUser.telegram_id == user_id)
            )
            return enabled is not False

    async def toggle_notifications(self, user_id: int) -> bool:
        async with self.sessions.begin() as session:
            user = await self._lock_user(session, user_id)
            user.notifications_enabled = not user.notifications_enabled
            if not user.notifications_enabled:
                await session.execute(
                    delete(Notification).where(
                        Notification.target_id == user_id, Notification.kind != "new_submission"
                    )
                )
            return user.notifications_enabled

    async def delete_user_data(self, user_id: int) -> None:
        async with self.sessions.begin() as session:
            user = await self._lock_user(session, user_id)
            # Foreign keys erase submissions, their audits and queued notifications.
            await session.delete(user)
            await session.execute(delete(Notification).where(Notification.target_id == user_id))

    async def cleanup(self) -> None:
        now = datetime.now(UTC)
        content_cutoff = now - timedelta(days=self.settings.retention_days)
        original_cutoff = now - timedelta(days=self.settings.original_text_retention_days)
        technical_cutoff = now - timedelta(days=self.settings.technical_retention_days)
        while True:
            async with self.sessions.begin() as session:
                has_submissions = (
                    select(Submission.id).where(Submission.user_id == BotUser.telegram_id).exists()
                )
                expired_content = (
                    select(Submission.id)
                    .where(
                        Submission.user_id == BotUser.telegram_id,
                        or_(
                            Submission.created_at < content_cutoff,
                            and_(
                                Submission.created_at < original_cutoff,
                                Submission.original_text.is_not(None),
                            ),
                        ),
                    )
                    .exists()
                )
                expired_notices = (
                    select(Notification.id)
                    .join(Submission, Submission.id == Notification.submission_id)
                    .where(
                        Submission.user_id == BotUser.telegram_id,
                        Notification.created_at < technical_cutoff,
                    )
                    .exists()
                )
                # Use the same owner-first lock order as review/delete/outbox. Active owners
                # are skipped and picked up at the next hourly run instead of blocking them.
                owners = list(
                    await session.scalars(
                        select(BotUser.telegram_id)
                        .where(
                            or_(
                                expired_content,
                                expired_notices,
                                and_(BotUser.last_seen_at < content_cutoff, ~has_submissions),
                            )
                        )
                        .order_by(BotUser.telegram_id)
                        .limit(100)
                        .with_for_update(skip_locked=True)
                    )
                )
                if not owners:
                    return
                owned_ids = select(Submission.id).where(Submission.user_id.in_(owners))
                await session.execute(
                    delete(Notification).where(
                        Notification.submission_id.in_(owned_ids),
                        Notification.created_at < technical_cutoff,
                    )
                )
                await session.execute(
                    update(Submission)
                    .where(
                        Submission.user_id.in_(owners),
                        Submission.created_at < original_cutoff,
                        Submission.original_text.is_not(None),
                    )
                    .values(original_text=None)
                )
                await session.execute(
                    delete(Submission).where(
                        Submission.user_id.in_(owners),
                        Submission.created_at < content_cutoff,
                    )
                )
                await session.execute(
                    delete(BotUser).where(
                        BotUser.telegram_id.in_(owners),
                        BotUser.last_seen_at < content_cutoff,
                        ~has_submissions,
                    )
                )
