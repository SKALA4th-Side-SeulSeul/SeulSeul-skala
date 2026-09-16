"""학생 영속 모델."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from seulseul.database import Base

if TYPE_CHECKING:
    from seulseul.checklists.model import ChecklistModel


@dataclass(frozen=True)
class Student:
    """가입한 Slack 학생의 식별자와 소속."""

    workspace_id: str
    slack_user_id: str
    display_name: str
    campus: str
    class_number: int


class StudentModel(Base):
    """Slack 사용자와 캠퍼스·반 소속을 저장한다."""

    __tablename__ = "students"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "slack_user_id",
            name="uq_students_workspace_slack_user",
        ),
        CheckConstraint("class_number > 0", name="ck_students_class_number_positive"),
        Index("ix_students_affiliation", "workspace_id", "campus", "class_number"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    workspace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    slack_user_id: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    campus: Mapped[str] = mapped_column(String(32), nullable=False)
    class_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
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
        back_populates="student",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
