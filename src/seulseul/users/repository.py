"""학생 저장소 구현."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seulseul.models import MAPPED_MODELS
from seulseul.users.model import Student, StudentModel

_MAPPED_MODELS = MAPPED_MODELS


class StudentRepository(Protocol):
    """학생 서비스가 사용하는 저장소 계약."""

    def save(self, student: Student) -> None: ...

    def delete(self, workspace_id: str, slack_user_id: str) -> bool: ...

    def get(self, workspace_id: str, slack_user_id: str) -> Student | None: ...

    def update_profile(self, previous: Student, current: Student) -> bool: ...


class InMemoryStudentRepository:
    """DB 없이 학생 도메인 동작을 검증하는 저장소."""

    def __init__(self) -> None:
        self._students: dict[tuple[str, str], Student] = {}
        self._lock = threading.Lock()

    def save(self, student: Student) -> None:
        with self._lock:
            self._students[(student.workspace_id, student.slack_user_id)] = student

    def delete(self, workspace_id: str, slack_user_id: str) -> bool:
        with self._lock:
            return self._students.pop((workspace_id, slack_user_id), None) is not None

    def get(self, workspace_id: str, slack_user_id: str) -> Student | None:
        with self._lock:
            return self._students.get((workspace_id, slack_user_id))

    def update_profile(self, previous: Student, current: Student) -> bool:
        key = (previous.workspace_id, previous.slack_user_id)
        with self._lock:
            if self._students.get(key) != previous:
                return False
            self._students[key] = current
            return True


class SqlAlchemyStudentRepository:
    """PostgreSQL에 가입 학생을 저장하고 삭제한다."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def recipient_ids(self, workspace_id: str) -> list[str]:
        """현재 가입한 학생만 워크스페이스별로 조회한다."""
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(StudentModel.slack_user_id)
                    .where(StudentModel.workspace_id == workspace_id)
                    .order_by(StudentModel.slack_user_id)
                )
            )

    def get(self, workspace_id: str, slack_user_id: str) -> Student | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(StudentModel).where(
                    StudentModel.workspace_id == workspace_id,
                    StudentModel.slack_user_id == slack_user_id,
                )
            )
            return (
                Student(
                    row.workspace_id, row.slack_user_id, row.real_name, row.campus, row.class_number
                )
                if row is not None
                else None
            )

    def update_profile(self, previous: Student, current: Student) -> bool:
        # UPSERT 금지: 조회 후 해지됐다면 재가입시키지 않는다. 기존 PK/완료 기록 유지.
        statement = (
            update(StudentModel)
            .where(
                StudentModel.workspace_id == previous.workspace_id,
                StudentModel.slack_user_id == previous.slack_user_id,
                StudentModel.real_name == previous.real_name,
                StudentModel.campus == previous.campus,
                StudentModel.class_number == previous.class_number,
            )
            .values(
                real_name=current.real_name,
                campus=current.campus,
                class_number=current.class_number,
            )
            .returning(StudentModel.id)
        )
        with self._session_factory() as session:
            changed = session.execute(statement).scalar_one_or_none() is not None
            session.commit()
            return changed

    def save(self, student: Student) -> None:
        statement = (
            insert(StudentModel)
            .values(
                workspace_id=student.workspace_id,
                slack_user_id=student.slack_user_id,
                real_name=student.real_name,
                campus=student.campus,
                class_number=student.class_number,
            )
            .on_conflict_do_update(
                constraint="uq_students_workspace_slack_user",
                set_={
                    StudentModel.real_name: student.real_name,
                    "campus": student.campus,
                    "class_number": student.class_number,
                    "updated_at": func.now(),
                },
            )
        )
        with self._session_factory() as session:
            session.execute(statement)
            session.commit()

    def delete(self, workspace_id: str, slack_user_id: str) -> bool:
        statement = (
            delete(StudentModel)
            .where(
                StudentModel.workspace_id == workspace_id,
                StudentModel.slack_user_id == slack_user_id,
            )
            .returning(StudentModel.id)
        )
        with self._session_factory() as session:
            deleted_id = session.execute(statement).scalar_one_or_none()
            session.commit()
            return deleted_id is not None
