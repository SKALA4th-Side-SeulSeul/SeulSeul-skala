"""학생 ORM 모델의 식별자와 소속 제약을 확인한다."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from seulseul.users.model import Student, StudentModel
from seulseul.users.repository import InMemoryStudentRepository, SqlAlchemyStudentRepository
from seulseul.users.service import (
    InvalidStudentRealNameError,
    StudentAffiliation,
    StudentService,
    parse_student_real_name,
)

WORKSPACE_ID = "T0000000001"
USER_ID = "U0000000001"


def test_student_model_has_workspace_user_identity_and_affiliation() -> None:
    table = StudentModel.__table__
    unique_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {"workspace_id", "slack_user_id", "display_name", "campus", "class_number"} <= set(
        table.columns.keys()
    )
    assert "uq_students_workspace_slack_user" in unique_constraints
    assert "ck_students_class_number_positive" in check_constraints


@pytest.mark.parametrize("class_number", range(1, 5))
def test_parse_student_real_name_accepts_classes_one_to_four(class_number: int) -> None:
    assert parse_student_real_name(f"4기_광주_{class_number}반_홍길동") == StudentAffiliation(
        class_number=class_number,
        student_name="홍길동",
    )


@pytest.mark.parametrize(
    "real_name",
    [
        "광주_3반_홍길동",
        "3기_광주_3반_홍길동",
        "4기_서울_3반_홍길동",
        "4기_광주_0반_홍길동",
        "4기_광주_5반_홍길동",
        "4기_광주_3반_",
        "4기_광주_3반_   ",
    ],
)
def test_parse_student_real_name_rejects_invalid_format(real_name: str) -> None:
    with pytest.raises(InvalidStudentRealNameError, match="Slack 성명.*4기_광주"):
        parse_student_real_name(real_name)


def test_student_service_enrolls_and_updates_same_slack_user() -> None:
    repository = InMemoryStudentRepository()
    service = StudentService(repository)

    first = service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_1반_홍길동")
    updated = service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_4반_홍길동")

    assert first.class_number == 1
    assert updated.class_number == 4
    assert updated.real_name == "4기_광주_4반_홍길동"
    assert repository.get(WORKSPACE_ID, USER_ID) == updated


def test_student_service_withdraws_personal_student_record() -> None:
    repository = InMemoryStudentRepository()
    service = StudentService(repository)
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_홍길동")

    assert service.withdraw(WORKSPACE_ID, USER_ID)
    assert repository.get(WORKSPACE_ID, USER_ID) is None
    assert not service.withdraw(WORKSPACE_ID, USER_ID)


def test_sqlalchemy_student_repository_upserts_student() -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    repository = SqlAlchemyStudentRepository(lambda: session)
    student = Student(
        workspace_id=WORKSPACE_ID,
        slack_user_id=USER_ID,
        real_name="4기_광주_3반_홍길동",
        campus="광주",
        class_number=3,
    )

    repository.save(student)

    statement = session.execute.call_args.args[0]
    parameters = statement.compile().params
    assert parameters["workspace_id"] == WORKSPACE_ID
    assert parameters["class_number"] == 3
    assert parameters["display_name"] == student.real_name
    session.commit.assert_called_once_with()


def test_sqlalchemy_student_repository_reports_deleted_student() -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.scalar_one_or_none.return_value = uuid4()
    repository = SqlAlchemyStudentRepository(lambda: session)

    assert repository.delete(WORKSPACE_ID, USER_ID)
    session.commit.assert_called_once_with()
