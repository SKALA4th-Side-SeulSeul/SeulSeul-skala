"""NoticeService가 새 공지만 골라 최신순으로 보관하고, AI 요약을 안전하게 붙이는지 확인한다."""

import logging
from typing import Any

import pytest

from seulseul.ai.client import AiClientError
from seulseul.notices.model import Notice
from seulseul.notices.service import NoticeService, is_new_notice_message


def channel_message(**overrides: Any) -> dict[str, Any]:
    message = {
        "type": "message",
        "channel": "C0000000001",
        "channel_type": "channel",
        "ts": "1757822400.000100",
        "text": "  9월 20일까지 과제 제출  ",
    }
    message.update(overrides)
    return message


class FakeSummarizer:
    """실제 AI 대신 정해진 요약을 돌려주거나 오류를 발생시킨다."""

    def __init__(self, summary: str = "", error: Exception | None = None) -> None:
        self.summary = summary
        self.error = error
        self.received_texts: list[str] = []

    def summarize(self, notice_text: str) -> str:
        self.received_texts.append(notice_text)
        if self.error is not None:
            raise self.error
        return self.summary


def test_record_channel_message_stores_trimmed_top_level_message() -> None:
    service = NoticeService()

    notice = service.record_channel_message(channel_message())

    expected = Notice(
        channel_id="C0000000001", message_ts="1757822400.000100", text="9월 20일까지 과제 제출"
    )
    assert notice == expected
    assert service.recent_notices(5) == [expected]


def test_private_channel_message_is_a_notice() -> None:
    assert is_new_notice_message(channel_message(channel_type="group"))


def test_thread_parent_message_is_a_notice() -> None:
    assert is_new_notice_message(channel_message(thread_ts="1757822400.000100"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"subtype": "message_changed"},
        {"subtype": "message_deleted"},
        {"subtype": "channel_join"},
        {"subtype": "bot_message"},
        {"bot_id": "B0000000001"},
        {"channel_type": "im"},
        {"channel_type": None},
        {"thread_ts": "1757822300.000001"},
        {"text": "   "},
        {"text": None},
    ],
)
def test_non_notice_messages_are_not_stored_or_summarized(overrides: dict[str, Any]) -> None:
    summarizer = FakeSummarizer(summary="요약")
    service = NoticeService(summarizer=summarizer)

    assert service.record_channel_message(channel_message(**overrides)) is None
    assert service.recent_notices(5) == []
    assert summarizer.received_texts == []


def test_record_channel_message_attaches_ai_summary_of_trimmed_text() -> None:
    summarizer = FakeSummarizer(summary="요약: 과제 제출\n할 일:\n- 제출\n마감: 9월 20일")
    service = NoticeService(summarizer=summarizer)

    notice = service.record_channel_message(channel_message())

    assert summarizer.received_texts == ["9월 20일까지 과제 제출"]
    assert notice is not None
    assert notice.summary == "요약: 과제 제출\n할 일:\n- 제출\n마감: 9월 20일"
    assert service.recent_notices(1) == [notice]


def test_record_channel_message_keeps_notice_when_ai_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    summarizer = FakeSummarizer(
        error=AiClientError("AI 서버가 오류를 반환했습니다. 상태 코드: 500")
    )
    service = NoticeService(summarizer=summarizer)

    with caplog.at_level(logging.WARNING):
        notice = service.record_channel_message(channel_message(text="홍길동 개인정보 공지"))

    assert notice is not None
    assert notice.summary is None
    assert service.recent_notices(1) == [notice]
    assert "공지 AI 요약 실패" in caplog.text
    assert "상태 코드: 500" in caplog.text
    assert "홍길동" not in caplog.text


def test_recent_notices_returns_newest_first_up_to_limit() -> None:
    service = NoticeService()
    for index in range(3):
        service.record_channel_message(channel_message(ts=f"{index}.0", text=f"공지 {index}"))

    assert [notice.text for notice in service.recent_notices(2)] == ["공지 2", "공지 1"]


def test_oldest_notice_is_dropped_when_storage_is_full() -> None:
    service = NoticeService(max_stored_notices=2)
    for index in range(3):
        service.record_channel_message(channel_message(ts=f"{index}.0", text=f"공지 {index}"))

    assert [notice.text for notice in service.recent_notices(5)] == ["공지 2", "공지 1"]


def test_invalid_sizes_raise_error_with_given_value() -> None:
    with pytest.raises(ValueError, match="전달된 값: 0"):
        NoticeService(max_stored_notices=0)

    with pytest.raises(ValueError, match="전달된 값: -1"):
        NoticeService().recent_notices(-1)
