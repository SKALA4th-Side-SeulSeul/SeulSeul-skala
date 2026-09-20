"""학생별 체크리스트 영속 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seulseul.database import Base

if TYPE_CHECKING:
    from seulseul.notices.model import NoticeModel
    from seulseul.users.model import StudentModel


class ChecklistModel(Base):
    """학생별 링크의 완료 상태. notice_id는 현재 표시할 원본을 가리킨다."""

    __tablename__ = "checklists"
    __table_args__ = (
        UniqueConstraint("student_id", "notice_id", name="uq_checklists_student_notice"),
        UniqueConstraint("student_id", "canonical_url", name="uq_checklists_student_link"),
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
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
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


class DailyChecklistMessageModel(Base):
    """학생별 하루 한 건의 DM 주소와 전송 상태. 해지 시 DB 기록도 삭제한다."""

    __tablename__ = "daily_checklist_messages"
    __table_args__ = (
        UniqueConstraint("student_id", "message_date", name="uq_daily_checklist_student_date"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'retry', 'uncertain')",
            name="ck_daily_checklist_status",
        ),
        CheckConstraint("page >= 0", name="ck_daily_checklist_page"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    student_id: Mapped[UUID] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    message_date: Mapped[date] = mapped_column(Date)
    dm_channel_id: Mapped[str | None] = mapped_column(String(32))
    message_ts: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    content_hash: Mapped[str | None] = mapped_column(String(64))
    show_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    page: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_token: Mapped[UUID | None] = mapped_column()
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


@dataclass(frozen=True)
class ChecklistRecipient:
    id: UUID
    workspace_id: str
    user_id: str
    class_number: int


@dataclass(frozen=True)
class ChecklistItem:
    id: UUID
    title: str
    summary: str
    deadline_at: datetime
    original_url: str
    source_permalink: str
    completed: bool


@dataclass(frozen=True)
class DailyChecklistBoard:
    id: UUID
    message_date: date
    items: tuple[ChecklistItem, ...]
    pending_count: int
    completed_count: int
    show_completed: bool
    page: int
    page_count: int
    refreshed_at: datetime


@dataclass(frozen=True)
class DeliveryClaim:
    token: UUID
    workspace_id: str
    user_id: str
    channel_id: str | None
    message_ts: str | None
    previous_hash: str | None
    board: DailyChecklistBoard


class ChecklistActionError(Exception):
    """가입·소유권·유효 기간 검증 실패. 사용자에게 안전하게 안내할 수 있다."""


class ChecklistDeliveryError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False, retry_after: float = 60) -> None:
        super().__init__(code)
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after
