"""학생 가입·해지와 Slack 성명의 소속 판별 규칙."""

import logging
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from threading import RLock

from seulseul.users.model import Student
from seulseul.users.repository import StudentRepository

STUDENT_REAL_NAME_PATTERN = re.compile(r"^4기_광주_(?P<class_number>[1-4])반_(?P<student_name>.+)$")
REAL_NAME_GUIDE = "4기_광주_<1~4>반_<이름>"
logger = logging.getLogger(__name__)


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
    if match is None or len(normalized_name) > 100 or "\x00" in normalized_name:
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
        on_withdraw: Callable[[str, bool], None] = lambda user, deleted: None,
        on_invalid_name: Callable[[str, str], None] = lambda user, guidance: None,
        profile_update: Callable[[str, str], AbstractContextManager[None]] = (
            lambda workspace, user: nullcontext()
        ),
    ) -> None:
        self._repository = repository
        self._on_change = on_change
        self._reset_messages = reset_messages
        self._on_withdraw = on_withdraw
        self._on_invalid_name = on_invalid_name
        self._profile_update = profile_update
        # 잠금 수는 고정하고 같은 학생의 가입·해지·현재 프로필 조회를 직렬화한다.
        self._profile_locks = tuple(RLock() for _ in range(64))

    def _profile_lock(self, workspace_id: str, user_id: str):
        return self._profile_locks[hash((workspace_id, user_id)) % len(self._profile_locks)]

    def sync_profile(
        self, workspace_id: str, user_id: str, get_real_name: Callable[[str], str]
    ) -> bool:
        """가입자만 현재 성명을 재조회한다. 이벤트 본문의 오래된 성명은 적용하지 않는다."""
        with self._profile_lock(workspace_id, user_id):
            previous = self._repository.get(workspace_id, user_id)
            if previous is None:
                return False
            real_name = get_real_name(user_id).strip()
            affiliation = parse_student_real_name(real_name)
            current = Student(workspace_id, user_id, real_name, "광주", affiliation.class_number)
            if current == previous:
                return False
            # 조회 중 체크리스트 전체를 막지 않고, DB 변경만 메시지 발송과 직렬화한다.
            with self._profile_update(workspace_id, user_id):
                changed = self._repository.update_profile(previous, current)
            if changed:
                self._on_change()
            return changed

    def enroll(self, workspace_id: str, slack_user_id: str, real_name: str) -> Student:
        with self._profile_lock(workspace_id, slack_user_id):
            return self._enroll(workspace_id, slack_user_id, real_name)

    def enroll_from_profile(
        self, workspace_id: str, user_id: str, get_real_name: Callable[[str], str]
    ) -> Student:
        with self._profile_lock(workspace_id, user_id):
            return self._enroll(workspace_id, user_id, get_real_name(user_id))

    def _enroll(self, workspace_id: str, slack_user_id: str, real_name: str) -> Student:
        """성명을 검증하고 학생을 새로 저장하거나 최신 소속으로 갱신한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        try:
            affiliation = parse_student_real_name(real_name)
        except InvalidStudentRealNameError:
            try:
                self._on_invalid_name(
                    slack_user_id,
                    "Slack 성명 형식이 맞지 않아 SeulSeul 연동을 시작하지 못했습니다.\n"
                    f"프로필의 표시 이름이 아닌 *성명*을 `{REAL_NAME_GUIDE}` 형식으로 "
                    "설정해 주세요. (예: `4기_광주_1반_홍길동`)\n"
                    "수정한 뒤 `/seulseul 시작`을 다시 입력해 주세요. "
                    "기존 가입 정보와 완료 기록은 변경하지 않았습니다.",
                )
            except Exception as error:
                logger.warning("성명 수정 안내 DM 전송 실패: type=%s", type(error).__name__)
            raise
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
        with self._profile_lock(workspace_id, slack_user_id):
            return self._withdraw(workspace_id, slack_user_id)

    def _withdraw(self, workspace_id: str, slack_user_id: str) -> bool:
        """학생 개인정보와 외래 키로 연결된 개인 체크리스트를 삭제한다."""
        if not workspace_id or not slack_user_id:
            raise ValueError("workspace_id와 slack_user_id는 비어 있을 수 없습니다.")
        with self._reset_messages(workspace_id, slack_user_id):
            deleted = self._repository.delete(workspace_id, slack_user_id)
            self._on_withdraw(slack_user_id, deleted)
        self._on_change()
        return deleted
