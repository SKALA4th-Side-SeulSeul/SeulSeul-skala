"""학생별 체크리스트 영속 모델."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seulseul.database import Base

if TYPE_CHECKING:
    from seulseul.notices.model import NoticeModel
    from seulseul.users.model import StudentModel


class ChecklistModel(Base):
    """학생 한 명에게 배정된 공지의 완료·삭제 상태를 저장한다."""

    __tablename__ = "checklists"
    __table_args__ = (
        UniqueConstraint("student_id", "notice_id", name="uq_checklists_student_notice"),
        Index("ix_checklists_student_active", "student_id", "deleted_at"),
        Index("ix_checklists_notice_id", "notice_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    student_id: Mapped[UUID] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"),
        nullable=False,
    )
    notice_id: Mapped[UUID] = mapped_column(
        ForeignKey("notices.id", ondelete="CASCADE"),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    student: Mapped[StudentModel] = relationship(back_populates="checklists")
    notice: Mapped[NoticeModel] = relationship(back_populates="checklists")
