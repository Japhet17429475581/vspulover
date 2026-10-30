from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "love_bot"


class Base(DeclarativeBase):
    metadata = MetaData(
        schema=SCHEMA,
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        },
    )


class BotUser(Base):
    __tablename__ = "bot_users"
    __table_args__ = (CheckConstraint("telegram_id > 0", name="positive_user"),)

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    submissions_count: Mapped[int] = mapped_column(Integer, server_default="0")


class Submission(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint("status IN ('pending','approved','rejected','published')", name="status"),
        CheckConstraint(
            "moderation_decision IN ('manual_review','auto_approved','rejected')", name="decision"
        ),
        CheckConstraint("char_length(normalized_text) BETWEEN 1 AND 500", name="text_length"),
        Index("ix_submissions_status_created", "status", "created_at", "id"),
        Index("ix_submissions_user_created", "user_id", "created_at"),
        Index("ix_submissions_created_at", "created_at"),
        Index("ix_submissions_duplicate", "user_id", "text_fingerprint", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.bot_users.telegram_id", ondelete="CASCADE"), nullable=False
    )
    request_key: Mapped[UUID] = mapped_column(Uuid, unique=True)
    original_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    text_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    moderation_decision: Mapped[str] = mapped_column(String(20))
    moderation_confidence: Mapped[float | None] = mapped_column(Float)
    moderation_flags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    moderation_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    rejection_reason: Mapped[str | None] = mapped_column(String(80))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel_message_id: Mapped[int | None] = mapped_column(BigInteger)


class ModerationAudit(Base):
    __tablename__ = "moderation_audit"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.submissions.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    moderator_id: Mapped[int | None] = mapped_column(BigInteger)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    details: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


class Notification(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        UniqueConstraint("submission_id", "target_id", "kind"),
        CheckConstraint("target_id > 0", name="private_recipient"),
        CheckConstraint("kind IN ('approved','rejected','new_submission')", name="kind"),
        Index("ix_notification_outbox_due", "completed_at", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey(f"{SCHEMA}.submissions.id", ondelete="CASCADE")
    )
    target_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
