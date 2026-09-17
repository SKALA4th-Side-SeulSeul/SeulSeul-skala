"""학생 가입·해지와 Slack 성명의 소속 판별 규칙."""

import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

from seulseul.users.model import Student
from seulseul.users.repository import StudentRepository

STUDENT_REAL_NAME_PATTERN = re.compile(r"^4기_광주_(?P<class_number>[1-4])반_(?P<student_name>.+)$")
REAL_NAME_GUIDE = "4기_광주_<1~4>반_<이름>"


class InvalidStudentRealNameError(ValueError):
    """Slack 성명으로 광주캠퍼스 반을 판별할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class StudentAffiliation:
    class_number: int
    student_name: str


def parse_student_real_name(real_name: str) -> StudentAffiliation:
    """`4기_광주_<1~4>반_<이름>` 성명에서 반과 이름을 추출한다."""
    normalized_name = real_name.strip()
    match = STUDENT_REAL_NAME_PATTERN.fullmatch(normalized_name)
    if match is None:
        raise InvalidStudentRealNameError(
            f"Slack 성명을 {REAL_NAME_GUIDE} 형식으로 설정해야 합니다."
        )
    student_name = match.group("student_name").strip()
    if not student_name:
        raise InvalidStudentRealNameError(
            f"Slack 성명을 {REAL_NAME_GUIDE} 형식으로 설정해야 합니다."
        )
    return StudentAffiliation(
        class_number=int(match.group("class_number")),
        student_name=student_name,
    )


class StudentService:
    def __init__(
        self,
        repository: StudentRepository,
        on_change: Callable[[], None] = lambda: None,
        *,
        reset_messages: Callable[
            [str, str], AbstractContextManager[None]
        ] = lambda workspace, user: nullcontext(),
    ) -> None:
        self._repository = repository
        self._on_change = on_change
        self._reset_messages = reset_messages

    def enroll(self, workspace_id: str, slack_user_id: str, real_name: str) -> Student:
        """성명을 검증하고 학생을 새로 저장하거나 최신 소속으로 갱신한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        affiliation = parse_student_real_name(real_name)
        student = Student(
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
            real_name=real_name.strip(),
            campus="광주",
            class_number=affiliation.class_number,
        )
        with self._reset_messages(workspace_id, slack_user_id):
            self._repository.save(student)
        self._on_change()
        return student

    def withdraw(self, workspace_id: str, slack_user_id: str) -> bool:
        """학생 개인정보와 외래 키로 연결된 개인 체크리스트를 삭제한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        with self._reset_messages(workspace_id, slack_user_id):
            deleted = self._repository.delete(workspace_id, slack_user_id)
        self._on_change()
        return deleted
