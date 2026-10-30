"""Create the isolated love_bot schema tables; never modify public tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None
SCHEMA = "love_bot"


def upgrade():
    op.create_table(
        "bot_users",
        sa.Column("telegram_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("notifications_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("submissions_count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint("telegram_id > 0", name="ck_bot_users_positive_user"),
        schema=SCHEMA,
    )
    op.create_table(
        "submissions",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{SCHEMA}.bot_users.telegram_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.Uuid(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=True),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("text_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("moderation_decision", sa.String(20), nullable=False),
        sa.Column("moderation_confidence", sa.Float(), nullable=True),
        sa.Column("moderation_flags", postgresql.JSONB(), nullable=False),
        sa.Column("moderation_reasons", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by", sa.BigInteger()),
        sa.Column("rejection_reason", sa.String(80)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("channel_message_id", sa.BigInteger()),
        sa.UniqueConstraint("request_key", name="uq_submissions_request_key"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','published')", name="ck_submissions_status"
        ),
        sa.CheckConstraint(
            "moderation_decision IN ('manual_review','auto_approved','rejected')",
            name="ck_submissions_decision",
        ),
        sa.CheckConstraint(
            "char_length(normalized_text) BETWEEN 1 AND 500", name="ck_submissions_text_length"
        ),
        schema=SCHEMA,
    )
    for name, cols in [
        ("ix_submissions_status_created", ["status", "created_at", "id"]),
        ("ix_submissions_user_created", ["user_id", "created_at"]),
        ("ix_submissions_created_at", ["created_at"]),
        ("ix_submissions_duplicate", ["user_id", "text_fingerprint", "created_at"]),
    ]:
        op.create_index(name, "submissions", cols, schema=SCHEMA)
    op.create_table(
        "moderation_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{SCHEMA}.submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("moderator_id", sa.BigInteger()),
        sa.Column(
            "timestamp", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_moderation_audit_submission_id", "moderation_audit", ["submission_id"], schema=SCHEMA
    )
    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{SCHEMA}.submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "submission_id", "target_id", "kind", name="uq_notification_outbox_submission_id"
        ),
        sa.CheckConstraint("target_id > 0", name="ck_notification_outbox_private_recipient"),
        sa.CheckConstraint(
            "kind IN ('approved','rejected','new_submission')", name="ck_notification_outbox_kind"
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_notification_outbox_due",
        "notification_outbox",
        ["completed_at", "next_attempt_at"],
        schema=SCHEMA,
    )


def downgrade():
    # Deliberately retain the schema itself, which may contain unrelated objects.
    for table in ("notification_outbox", "moderation_audit", "submissions", "bot_users"):
        op.drop_table(table, schema=SCHEMA)
