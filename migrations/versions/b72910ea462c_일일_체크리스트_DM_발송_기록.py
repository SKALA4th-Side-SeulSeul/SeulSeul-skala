"""일일 체크리스트 DM 발송 기록.

Revision ID: b72910ea462c
Revises: 65d23b502c33
"""

import sqlalchemy as sa
from alembic import op

revision = "b72910ea462c"
down_revision = "65d23b502c33"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_checklist_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("message_date", sa.Date(), nullable=False),
        sa.Column("dm_channel_id", sa.String(32)),
        sa.Column("message_ts", sa.String(32)),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("show_completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("page", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(255)),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("student_id", "message_date", name="uq_daily_checklist_student_date"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'retry', 'uncertain')",
            name="ck_daily_checklist_status",
        ),
        sa.CheckConstraint("page >= 0", name="ck_daily_checklist_page"),
    )


def downgrade() -> None:
    op.drop_table("daily_checklist_messages")
