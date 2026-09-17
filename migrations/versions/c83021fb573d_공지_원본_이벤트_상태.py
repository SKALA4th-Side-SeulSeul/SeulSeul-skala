"""공지 원본 이벤트 상태.

Revision ID: c83021fb573d
Revises: b72910ea462c
"""

import sqlalchemy as sa
from alembic import op

revision = "c83021fb573d"
down_revision = "b72910ea462c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notice_sources",
        sa.Column("workspace_id", sa.String(32), nullable=False),
        sa.Column("channel_id", sa.String(32), nullable=False),
        sa.Column("message_ts", sa.String(32), nullable=False),
        sa.Column("revision", sa.Numeric(20, 6), nullable=False),
        sa.Column("deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("applied", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "channel_id", "message_ts"),
    )
    # 기존 링크 기록은 유지하고, 원본 게시 시각을 초기 이벤트 버전으로 등록한다.
    # 링크 삭제 상태만으로 원본 자체의 삭제를 추정하지 않는다.
    op.execute(
        sa.text("""
        INSERT INTO notice_sources
            (workspace_id, channel_id, message_ts, revision, deleted, applied)
        SELECT DISTINCT workspace_id, channel_id, message_ts,
               CAST(message_ts AS NUMERIC(20, 6)), false, true
        FROM notices
    """)
    )


def downgrade() -> None:
    op.drop_table("notice_sources")
