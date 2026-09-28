"""학생별 마지막 마감 알림 상태. 기존 완료 기록은 유지한다."""

import sqlalchemy as sa
from alembic import op

revision = "e05243bd795f"
down_revision = "d94132ac684e"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in (
        ("reminder_status", sa.String(16)),
        ("reminder_deadline_at", sa.DateTime(timezone=True)),
        ("reminder_channel_id", sa.String(32)),
        ("reminder_message_ts", sa.String(32)),
        ("reminder_hash", sa.String(64)),
        ("reminder_retry_at", sa.DateTime(timezone=True)),
    ):
        op.add_column("checklists", sa.Column(name, kind, nullable=True))


def downgrade():
    for name in (
        "reminder_retry_at",
        "reminder_hash",
        "reminder_message_ts",
        "reminder_channel_id",
        "reminder_deadline_at",
        "reminder_status",
    ):
        op.drop_column("checklists", name)
