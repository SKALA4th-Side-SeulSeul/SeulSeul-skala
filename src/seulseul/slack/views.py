"""Slack에 보여 줄 메시지 문구를 만든다. Slack API 호출이나 업무 규칙은 두지 않는다."""

from collections.abc import Sequence

from seulseul.notices.model import Notice

# 응답 메시지가 지나치게 길어지지 않도록 공지 원문 하나당 보여 줄 최대 글자 수.
NOTICE_PREVIEW_MAX_CHARS = 300


def build_recent_notices_text(user_id: str, notices: Sequence[Notice]) -> str:
    """최근 공지 목록 응답 문구.

    AI 요약이 있으면 요약을, 없으면 원문 미리보기를 보여 준다.
    채널은 Slack 채널 링크(<#채널ID>)로 표시한다.
    """
    if not notices:
        return (
            f"<@{user_id}> 아직 수집된 공지가 없습니다. "
            "봇이 초대된 채널에 메시지를 올린 뒤 다시 시도하세요."
        )

    lines = [f"<@{user_id}> 최근 공지 {len(notices)}건입니다."]
    for index, notice in enumerate(notices, start=1):
        if notice.summary:
            lines.append(f"{index}. <#{notice.channel_id}>\n{notice.summary}")
        else:
            lines.append(f"{index}. <#{notice.channel_id}> {build_notice_preview(notice.text)}")
    return "\n".join(lines)


def build_notice_preview(text: str) -> str:
    """줄바꿈과 연속 공백을 한 칸으로 합치고 최대 글자 수를 넘으면 자른다."""
    single_line = " ".join(text.split())
    if len(single_line) <= NOTICE_PREVIEW_MAX_CHARS:
        return single_line
    return single_line[:NOTICE_PREVIEW_MAX_CHARS] + "…"
