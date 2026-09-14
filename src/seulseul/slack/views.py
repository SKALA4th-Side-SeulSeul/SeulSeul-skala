"""Slack에 보여 줄 메시지 문구를 만든다."""

from collections.abc import Sequence

from seulseul.notices.model import Notice


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
