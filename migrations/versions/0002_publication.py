"""Add durable publication queue and shared rate limit; retain existing submissions."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None
SCHEMA = "love_bot"


def upgrade():
    op.create_table(
        "publication_jobs",
        sa.Column(
            "submission_id",
            sa.BigInteger(),
            sa.ForeignKey(f"{SCHEMA}.submissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(80)),
        sa.CheckConstraint("channel_id < 0", name="ck_publication_jobs_channel"),
        sa.CheckConstraint(
            "status IN ('queued','sending','published','failed','uncertain')",
            name="ck_publication_jobs_status",
        ),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_publication_jobs_due", "publication_jobs", ["status", "next_attempt_at"], schema=SCHEMA
    )
    op.create_table(
        "publication_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "next_send_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1", name="ck_publication_state_singleton"),
        schema=SCHEMA,
    )
    op.execute("INSERT INTO love_bot.publication_state (id) VALUES (1)")


def downgrade():
    op.drop_table("publication_jobs", schema=SCHEMA)
    op.drop_table("publication_state", schema=SCHEMA)
