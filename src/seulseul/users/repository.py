"""학생 저장소 구현."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seulseul.models import MAPPED_MODELS
from seulseul.users.model import Student, StudentModel

_MAPPED_MODELS = MAPPED_MODELS


class StudentRepository(Protocol):
    """학생 서비스가 사용하는 저장소 계약."""

    def save(self, student: Student) -> None: ...

    def delete(self, workspace_id: str, slack_user_id: str) -> bool: ...


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


class SqlAlchemyStudentRepository:
    """PostgreSQL에 가입 학생을 저장하고 삭제한다."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

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
