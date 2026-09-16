"""공지 도메인의 메모리 구조와 영속 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seulseul.ai.model import NoticeAnalysis
from seulseul.database import Base

if TYPE_CHECKING:
    from seulseul.checklists.model import ChecklistModel

ProcessingStatus = Literal["processed", "processing_failed", "ai_disabled"]


@dataclass(frozen=True)
class Notice:
    workspace_id: str
    channel_id: str
    message_ts: str
    text: str
    original_url: str
    canonical_url: str
    source_permalink: str
    posted_at: datetime
    processing_status: ProcessingStatus
    analysis: NoticeAnalysis | None = None
    last_error: str | None = None


class NoticeModel(Base):
    """Slack 링크 하나와 AI 분석 결과를 저장한다."""

    __tablename__ = "notices"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "canonical_url",
            name="uq_notices_workspace_canonical_url",
        ),
        UniqueConstraint(
            "workspace_id",
            "channel_id",
            "message_ts",
            "canonical_url",
            name="uq_notices_source_link",
        ),
        CheckConstraint(
            "processing_status IN ('processed', 'processing_failed', 'ai_disabled')",
            name="ck_notices_processing_status",
        ),
        CheckConstraint("retry_count >= 0", name="ck_notices_retry_count_nonnegative"),
        Index("ix_notices_source_message", "workspace_id", "channel_id", "message_ts"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    source_permalink: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    summary: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_source_text: Mapped[str | None] = mapped_column(Text)
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False)
    retry_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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

    checklists: Mapped[list[ChecklistModel]] = relationship(
        back_populates="notice",
        passive_deletes=True,
    )
