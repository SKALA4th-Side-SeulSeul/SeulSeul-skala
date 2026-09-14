"""공지 수집 규칙.

Slack 채널 메시지 이벤트 중 새 공지로 볼 메시지만 골라 최신순으로 보관한다.
AI 요약기가 주어지면 저장하기 전에 요약을 붙이고, 요약에 실패해도 원문은 저장한다.
연결 테스트용 임시 구현이라 메모리에만 저장하며, 봇을 다시 실행하면 비워진다.
SLACK_NOTICE_CHANNELS로 채널을 거르는 기능은 아직 없어, 봇이 초대된 채널의 메시지를 모두 받는다.
"""

import logging
import threading
from collections import deque
from collections.abc import Mapping
from typing import Any, Protocol

from seulseul.ai.client import AiClientError
from seulseul.notices.model import Notice

logger = logging.getLogger(__name__)

DEFAULT_MAX_STORED_NOTICES = 50
# 공개 채널(channel)과 비공개 채널(group) 메시지만 공지로 본다. DM(im) 등은 제외한다.
NOTICE_CHANNEL_TYPES = frozenset({"channel", "group"})


class NoticeSummarizerProtocol(Protocol):
    def summarize(self, notice_text: str) -> str: ...


def is_new_notice_message(event: Mapping[str, Any]) -> bool:
    """메시지 이벤트가 새 공지로 볼 수 있는 일반 채널 메시지인지 판단한다."""
    # subtype이 있으면 수정·삭제·입장 알림·봇 메시지 등이라 새 공지가 아니다.
    if event.get("subtype") is not None or event.get("bot_id"):
        return False
    if event.get("channel_type") not in NOTICE_CHANNEL_TYPES:
        return False
    # 스레드 댓글은 공지 본문이 아니다. 스레드를 시작한 원글은 thread_ts와 ts가 같다.
    thread_ts = event.get("thread_ts")
    if thread_ts is not None and thread_ts != event.get("ts"):
        return False
    return (
        bool(event.get("channel"))
        and bool(event.get("ts"))
        and bool(str(event.get("text") or "").strip())
    )


class NoticeService:
    def __init__(
        self,
        max_stored_notices: int = DEFAULT_MAX_STORED_NOTICES,
        summarizer: NoticeSummarizerProtocol | None = None,
    ) -> None:
        if max_stored_notices < 1:
            raise ValueError(
                f"max_stored_notices는 1 이상이어야 합니다. 전달된 값: {max_stored_notices}"
            )
        self._notices: deque[Notice] = deque(maxlen=max_stored_notices)
        self._summarizer = summarizer
        # Bolt는 리스너를 여러 스레드에서 실행하므로 저장과 조회를 잠금으로 보호한다.
        self._lock = threading.Lock()

    def record_channel_message(self, event: Mapping[str, Any]) -> Notice | None:
        """새 공지로 볼 메시지면 요약을 붙여 저장하고 반환한다. 아니면 None을 반환한다."""
        if not is_new_notice_message(event):
            return None

        channel_id = event["channel"]
        message_ts = event["ts"]
        text = str(event["text"]).strip()
        notice = Notice(
            channel_id=channel_id,
            message_ts=message_ts,
            text=text,
            summary=self._summarize(channel_id, message_ts, text),
        )
        with self._lock:
            self._notices.appendleft(notice)
        return notice

    def recent_notices(self, limit: int) -> list[Notice]:
        """최신 공지부터 최대 limit건을 반환한다."""
        if limit < 1:
            raise ValueError(f"limit은 1 이상이어야 합니다. 전달된 값: {limit}")
        with self._lock:
            return list(self._notices)[:limit]

    def _summarize(self, channel_id: str, message_ts: str, text: str) -> str | None:
        if self._summarizer is None:
            return None
        try:
            return self._summarizer.summarize(text)
        except AiClientError as error:
            # 공지 원문은 개인정보가 있을 수 있어 로그에 남기지 않는다.
            logger.warning(
                "공지 AI 요약 실패: channel=%s ts=%s 원인=%s", channel_id, message_ts, error
            )
            return None
