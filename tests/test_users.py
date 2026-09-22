"""학생 ORM 모델의 식별자와 소속 제약을 확인한다."""

from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine
from sqlalchemy.orm import sessionmaker

from seulseul.database import Base
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


@pytest.fixture(params=["memory", "sql"])
def profile_repository(request):
    if request.param == "memory":
        yield InMemoryStudentRepository()
    else:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        yield SqlAlchemyStudentRepository(sessionmaker(engine))
        engine.dispose()


@pytest.mark.parametrize("previous,current", [(3, 1), (2, 4)])
def test_profile_sync_updates_existing_student_without_enroll_or_message_reset(
    profile_repository, previous, current
):
    service = StudentService(profile_repository)
    service.enroll(WORKSPACE_ID, USER_ID, f"4기_광주_{previous}반_가상학생")
    cleanup, notify = MagicMock(), MagicMock()
    service = StudentService(profile_repository, reset_messages=cleanup, on_change=notify)
    fetch = MagicMock(return_value=f"4기_광주_{current}반_가상학생")
    assert service.sync_profile(WORKSPACE_ID, USER_ID, fetch)
    assert profile_repository.get(WORKSPACE_ID, USER_ID).class_number == current
    notify.assert_called_once_with()
    cleanup.assert_not_called()
    assert not service.sync_profile(WORKSPACE_ID, USER_ID, fetch)
    notify.assert_called_once_with()


def test_profile_sync_does_not_fetch_or_enroll_unregistered_students(profile_repository):
    fetch = MagicMock()
    service = StudentService(profile_repository)
    assert not service.sync_profile(WORKSPACE_ID, USER_ID, fetch)
    fetch.assert_not_called()


def test_student_service_returns_student_name_for_operation_logs(profile_repository):
    service = StudentService(profile_repository)
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_홍길동")

    assert service.display_name(WORKSPACE_ID, USER_ID) == "홍길동"
    assert service.display_name(WORKSPACE_ID, "UOTHER") is None


def test_profile_sync_never_recreates_a_student_deleted_during_lookup(profile_repository):
    service = StudentService(profile_repository)
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_가상학생")

    def fetch(user):
        profile_repository.delete(WORKSPACE_ID, user)
        return "4기_광주_1반_가상학생"

    assert not service.sync_profile(WORKSPACE_ID, USER_ID, fetch)
    assert profile_repository.get(WORKSPACE_ID, USER_ID) is None


@pytest.mark.parametrize("failure", ["invalid", "lookup"])
def test_profile_sync_failure_preserves_existing_student(profile_repository, failure):
    service = StudentService(profile_repository)
    original = service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_가상학생")
    fetch = MagicMock(return_value="invalid")
    if failure == "lookup":
        fetch.side_effect = RuntimeError("network")
    with pytest.raises((InvalidStudentRealNameError, RuntimeError)):
        service.sync_profile(WORKSPACE_ID, USER_ID, fetch)
    assert profile_repository.get(WORKSPACE_ID, USER_ID) == original


def test_commands_cleanup_inside_guard_and_invalid_profile_does_not_delete():
    repository = InMemoryStudentRepository()
    calls = []

    @contextmanager
    def reset(workspace, user):
        calls.append((workspace, user, "before"))
        yield
        calls.append((workspace, user, "after"))

    service = StudentService(repository, reset_messages=reset)
    with pytest.raises(InvalidStudentRealNameError):
        service.enroll(WORKSPACE_ID, USER_ID, "invalid")
    assert not calls
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_가상학생")
    service.withdraw(WORKSPACE_ID, USER_ID)
    service.withdraw(WORKSPACE_ID, USER_ID)
    assert calls == [
        (WORKSPACE_ID, USER_ID, phase) for _ in range(3) for phase in ("before", "after")
    ]


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


def test_invalid_name_notifies_without_resetting_existing_student():
    repository = InMemoryStudentRepository()
    original = StudentService(repository).enroll(WORKSPACE_ID, USER_ID, "4기_광주_1반_가상학생")
    reset = MagicMock()
    notify = MagicMock()
    service = StudentService(repository, reset_messages=reset, on_invalid_name=notify)
    with pytest.raises(InvalidStudentRealNameError):
        service.enroll(WORKSPACE_ID, USER_ID, "형식 불일치")
    reset.assert_not_called()
    assert repository.get(WORKSPACE_ID, USER_ID) == original
    user, guidance = notify.call_args.args
    assert user == USER_ID
    assert "성명" in guidance and "4기_광주_<1~4>반_<이름>" in guidance
    assert "/seulseul 시작" in guidance


def test_invalid_name_dm_failure_does_not_expose_content_or_change_student(caplog):
    repository = InMemoryStudentRepository()
    notify = MagicMock(side_effect=RuntimeError("sensitive token and name"))
    service = StudentService(repository, on_invalid_name=notify)
    with pytest.raises(InvalidStudentRealNameError):
        service.enroll(WORKSPACE_ID, USER_ID, "형식 불일치")
    assert repository.get(WORKSPACE_ID, USER_ID) is None
    assert "RuntimeError" in caplog.text
    assert "sensitive token" not in caplog.text


def test_withdrawal_notification_follows_deletion_and_start_has_no_notification():
    repository = InMemoryStudentRepository()
    seen = []

    def notify(user, deleted):
        assert repository.get(WORKSPACE_ID, user) is None
        seen.append((user, deleted))

    service = StudentService(repository, on_withdraw=notify)
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_가상학생")
    assert seen == []
    service.withdraw(WORKSPACE_ID, USER_ID)
    service.withdraw(WORKSPACE_ID, USER_ID)
    assert seen == [(USER_ID, True), (USER_ID, False)]


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


def test_student_changes_notify_worker_after_persistence() -> None:
    repository = InMemoryStudentRepository()
    observed = []
    service = StudentService(
        repository, on_change=lambda: observed.append(repository.get(WORKSPACE_ID, USER_ID))
    )
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_홍길동")
    service.withdraw(WORKSPACE_ID, USER_ID)
    assert observed[0] is not None and observed[1] is None
