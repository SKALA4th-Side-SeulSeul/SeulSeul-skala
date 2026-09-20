"""채널별 원본과 학생별 링크.

Revision ID: d94132ac684e
Revises: c83021fb573d
"""

import sqlalchemy as sa
from alembic import op

revision = "d94132ac684e"
down_revision = "c83021fb573d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("checklists", sa.Column("canonical_url", sa.String(2048), nullable=True))
    op.execute("""
        UPDATE checklists AS c SET canonical_url = n.canonical_url
        FROM notices AS n WHERE c.notice_id = n.id
    """)
    op.alter_column("checklists", "canonical_url", nullable=False)
    op.create_unique_constraint(
        "uq_checklists_student_link", "checklists", ["student_id", "canonical_url"]
    )
    op.drop_constraint("uq_notices_workspace_canonical_url", "notices", type_="unique")
    op.create_index(
        "ix_notices_workspace_canonical_url", "notices", ["workspace_id", "canonical_url"]
    )


def downgrade() -> None:
    # 원본을 삭제해 옛 제약에 맞추지 않는다. 중복이 있으면 명시적으로 중단한다.
    connection = op.get_bind()
    duplicate = connection.scalar(
        sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM notices GROUP BY workspace_id, canonical_url HAVING count(*) > 1
        )
    """)
    )
    if duplicate:
        raise RuntimeError(
            "여러 원본이 저장되어 자동 downgrade할 수 없습니다. 데이터 보존 계획이 필요합니다."
        )
    op.drop_index("ix_notices_workspace_canonical_url", table_name="notices")
    op.create_unique_constraint(
        "uq_notices_workspace_canonical_url", "notices", ["workspace_id", "canonical_url"]
    )
    op.drop_constraint("uq_checklists_student_link", "checklists", type_="unique")
    op.drop_column("checklists", "canonical_url")
