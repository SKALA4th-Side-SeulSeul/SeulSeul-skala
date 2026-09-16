"""Slack에 보여 줄 메시지 문구를 만든다."""

from collections.abc import Sequence

from seulseul.notices.model import Notice
from seulseul.users.model import Student
from seulseul.users.service import DISPLAY_NAME_GUIDE


def build_recent_notices_text(user_id: str, notices: Sequence[Notice]) -> str:
    """원문 전체를 노출하지 않고 분석 결과와 Slack 원문 링크를 보여 준다."""
    if not notices:
        return f"<@{user_id}> 아직 처리된 공지가 없습니다."

    lines = [f"<@{user_id}> 최근 공지 {len(notices)}건입니다."]
    for index, notice in enumerate(notices, start=1):
        source_link = f"<{notice.source_permalink}|Slack 원문 보기>"
        if notice.processing_status == "processed" and notice.analysis is not None:
            deadline = notice.analysis.deadline_at.strftime("%Y-%m-%d %H:%M")
            lines.append(
                f"{index}. {notice.analysis.title}\n"
                f"{notice.analysis.summary}\n"
                f"마감: {deadline}\n"
                f"제출 링크: <{notice.original_url}|열기> · {source_link}"
            )
        elif notice.processing_status == "processing_failed":
            lines.append(f"{index}. AI 분석 실패 · {source_link}")
        else:
            lines.append(f"{index}. AI 분석 전 공지 · {source_link}")
    return "\n".join(lines)


def build_enrollment_success_text(user_id: str, student: Student) -> str:
    return f"<@{user_id}> SeulSeul 가입이 완료되었습니다. 소속: 광주 {student.class_number}반"


def build_invalid_display_name_text(user_id: str) -> str:
    return (
        f"<@{user_id}> 가입하려면 Slack 표시 이름을 "
        f"`{DISPLAY_NAME_GUIDE}` 형식으로 설정해 주세요. 예: `4기_광주_3반_홍길동`"
    )


def build_withdrawal_text(user_id: str, deleted: bool) -> str:
    if deleted:
        return f"<@{user_id}> 알림이 해지되었고 개인 체크리스트가 삭제되었습니다."
    return f"<@{user_id}> 현재 가입된 정보가 없습니다."


def build_command_help_text(user_id: str) -> str:
    return (
        f"<@{user_id}> `/seulseul 시작`으로 가입하고, "
        "`/seulseul 해지`로 알림을 해지할 수 있습니다. "
        "최근 공지는 `/seulseul` 또는 `/seulseul 공지`로 확인하세요."
    )
