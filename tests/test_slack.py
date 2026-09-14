"""공지 수집·조회용 Slack 핸들러와 응답 문구가 실제 Slack 없이 올바르게 동작하는지 확인한다."""

import logging
from typing import Any

import pytest
from slack_bolt import App

from seulseul.notices.model import Notice
from seulseul.notices.service import NoticeService
from seulseul.slack.handlers import (
    ANY_SLASH_COMMAND,
    create_message_event_handler,
    create_recent_notices_command_handler,
    register_handlers,
)
from seulseul.slack.views import (
    NOTICE_PREVIEW_MAX_CHARS,
    build_notice_preview,
    build_recent_notices_text,
)

COMMAND = {"command": "/seulseul-test", "user_id": "U0000000001", "channel_id": "D0000000001"}


class RecordingAck:
    """Bolt의 ack 함수 대신 호출 인자를 기록한다."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class FixedSummarizer:
    def summarize(self, notice_text: str) -> str:
        return "요약: 과제 제출\n할 일:\n- 과제 제출\n마감: 9월 20일"


def channel_message(text: str, ts: str) -> dict[str, Any]:
    return {
        "type": "message",
        "channel": "C0000000001",
        "channel_type": "channel",
        "ts": ts,
        "text": text,
    }


def test_command_acks_with_empty_guide_when_no_notices() -> None:
    ack = RecordingAck()
    handler = create_recent_notices_command_handler(NoticeService())

    handler(ack, COMMAND, logging.getLogger("test"))

    assert len(ack.calls) == 1
    (text,), _ = ack.calls[0]
    assert text.startswith("<@U0000000001> 아직 수집된 공지가 없습니다.")


def test_command_shows_notice_recorded_from_channel_message() -> None:
    notice_service = NoticeService()
    record_message = create_message_event_handler(notice_service)
    handle_command = create_recent_notices_command_handler(notice_service)
    ack = RecordingAck()

    record_message(
        channel_message("9월 20일까지 과제 제출", "1757822400.000100"), logging.getLogger("test")
    )
    handle_command(ack, COMMAND, logging.getLogger("test"))

    (text,), _ = ack.calls[0]
    assert text == "<@U0000000001> 최근 공지 1건입니다.\n1. <#C0000000001> 9월 20일까지 과제 제출"


def test_command_shows_ai_summary_instead_of_original_text() -> None:
    notice_service = NoticeService(summarizer=FixedSummarizer())
    record_message = create_message_event_handler(notice_service)
    handle_command = create_recent_notices_command_handler(notice_service)
    ack = RecordingAck()

    record_message(
        channel_message("원문: 9월 20일까지 과제 제출", "1757822400.000100"),
        logging.getLogger("test"),
    )
    handle_command(ack, COMMAND, logging.getLogger("test"))

    (text,), _ = ack.calls[0]
    assert text.splitlines() == [
        "<@U0000000001> 최근 공지 1건입니다.",
        "1. <#C0000000001>",
        "요약: 과제 제출",
        "할 일:",
        "- 과제 제출",
        "마감: 9월 20일",
    ]


def test_message_event_log_excludes_message_text(caplog: pytest.LogCaptureFixture) -> None:
    handler = create_message_event_handler(NoticeService(summarizer=FixedSummarizer()))
    event = channel_message("홍길동 010-0000-0000 개인정보가 포함된 공지", "1757822400.000100")

    with caplog.at_level(logging.INFO):
        handler(event, logging.getLogger("test"))

    assert "channel=C0000000001" in caplog.text
    assert "stored=True summarized=True" in caplog.text
    assert "홍길동" not in caplog.text


def test_recent_notices_text_lists_notices_in_given_order() -> None:
    notices = [
        Notice(channel_id="C0000000002", message_ts="2.0", text="두 번째 공지"),
        Notice(channel_id="C0000000001", message_ts="1.0", text="첫 번째 공지"),
    ]

    text = build_recent_notices_text("U0000000001", notices)

    assert text.splitlines() == [
        "<@U0000000001> 최근 공지 2건입니다.",
        "1. <#C0000000002> 두 번째 공지",
        "2. <#C0000000001> 첫 번째 공지",
    ]


def test_notice_preview_joins_lines_and_truncates_long_text() -> None:
    assert build_notice_preview("첫 줄\n\n  둘째 줄") == "첫 줄 둘째 줄"

    long_text = "가" * (NOTICE_PREVIEW_MAX_CHARS + 10)
    assert build_notice_preview(long_text) == "가" * NOTICE_PREVIEW_MAX_CHARS + "…"


@pytest.mark.parametrize("command_name", ["/seulseul", "/seulseul-kgj", "/test_bot"])
def test_any_slash_command_matches_developer_specific_names(command_name: str) -> None:
    assert ANY_SLASH_COMMAND.match(command_name)


def test_any_slash_command_rejects_text_without_slash() -> None:
    assert not ANY_SLASH_COMMAND.match("seulseul")


def test_register_handlers_accepts_socket_mode_app() -> None:
    # token_verification_enabled=False로 auth.test 네트워크 호출을 막는다.
    app = App(token="xoxb-test-token", token_verification_enabled=False)

    register_handlers(app, NoticeService())
