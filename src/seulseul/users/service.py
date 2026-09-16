"""학생 가입·해지와 Slack 표시 이름의 소속 판별 규칙."""

import re
from dataclasses import dataclass

from seulseul.users.model import Student
from seulseul.users.repository import StudentRepository

STUDENT_DISPLAY_NAME_PATTERN = re.compile(
    r"^4기_광주_(?P<class_number>[1-4])반_(?P<student_name>.+)$"
)
DISPLAY_NAME_GUIDE = "4기_광주_<1~4>반_<이름>"


class InvalidStudentDisplayNameError(ValueError):
    """Slack 표시 이름으로 광주캠퍼스 반을 판별할 수 없을 때 발생한다."""


@dataclass(frozen=True)
class StudentAffiliation:
    class_number: int
    student_name: str


def parse_student_display_name(display_name: str) -> StudentAffiliation:
    """`4기_광주_<1~4>반_<이름>` 표시 이름에서 반과 이름을 추출한다."""
    normalized_name = display_name.strip()
    match = STUDENT_DISPLAY_NAME_PATTERN.fullmatch(normalized_name)
    if match is None:
        raise InvalidStudentDisplayNameError(
            f"Slack 표시 이름을 {DISPLAY_NAME_GUIDE} 형식으로 설정해야 합니다."
        )
    student_name = match.group("student_name").strip()
    if not student_name:
        raise InvalidStudentDisplayNameError(
            f"Slack 표시 이름을 {DISPLAY_NAME_GUIDE} 형식으로 설정해야 합니다."
        )
    return StudentAffiliation(
        class_number=int(match.group("class_number")),
        student_name=student_name,
    )


class StudentService:
    def __init__(self, repository: StudentRepository) -> None:
        self._repository = repository

    def enroll(self, workspace_id: str, slack_user_id: str, display_name: str) -> Student:
        """표시 이름을 검증하고 학생을 새로 저장하거나 최신 소속으로 갱신한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        affiliation = parse_student_display_name(display_name)
        student = Student(
            workspace_id=workspace_id,
            slack_user_id=slack_user_id,
            display_name=display_name.strip(),
            campus="광주",
            class_number=affiliation.class_number,
        )
        self._repository.save(student)
        return student

    def withdraw(self, workspace_id: str, slack_user_id: str) -> bool:
        """학생 개인정보와 외래 키로 연결된 개인 체크리스트를 삭제한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        return self._repository.delete(workspace_id, slack_user_id)
