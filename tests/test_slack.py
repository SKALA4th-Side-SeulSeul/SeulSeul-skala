"""Slack 핸들러와 사용자 응답을 실제 Slack API 없이 검증한다."""

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from slack_bolt import App

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.model import Notice, ProcessingStatus
from seulseul.notices.service import NoticeService
from seulseul.slack.client import MessagePermalinkError, SlackWebApiClient
from seulseul.slack.handlers import (
    ANY_SLASH_COMMAND,
    create_message_event_handler,
    create_recent_notices_command_handler,
    register_handlers,
)
from seulseul.slack.views import build_recent_notices_text

CHANNEL_ID = "C0000000001"
WORKSPACE_ID = "T0000000001"
MESSAGE_TS = "1789344000.000100"
PERMALINK = "https://workspace.slack.com/archives/C0000000001/p1789344000000100"
COMMAND = {"command": "/seulseul-test", "user_id": "U0000000001", "channel_id": "D1"}
SEOUL = ZoneInfo("Asia/Seoul")


class RecordingAck:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class FakePermalinkProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get_message_permalink(self, channel_id: str, message_ts: str) -> str:
        self.calls.append((channel_id, message_ts))
        return PERMALINK


class FailingPermalinkProvider:
    def get_message_permalink(self, channel_id: str, message_ts: str) -> str:
        raise MessagePermalinkError("조회 실패")


class FakeSlackWebClient:
    def __init__(self, response: dict[str, str]) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def chat_getPermalink(self, **kwargs: str) -> dict[str, str]:
        self.calls.append(kwargs)
        return self.response


def channel_message(**overrides: Any) -> dict[str, Any]:
    event = {
        "type": "message",
        "channel": CHANNEL_ID,
        "channel_type": "channel",
        "ts": MESSAGE_TS,
        "text": "비밀 원문 9월 20일까지 https://forms.example.test/task",
        "user": "UWRITER",
    }
    event.update(overrides)
    return event


def notice(status: ProcessingStatus = "processed") -> Notice:
    analysis = (
        NoticeAnalysis(
            title="과제 제출",
            summary="폼으로 과제를 제출합니다.",
            deadline_at=datetime(2026, 9, 20, 23, 59, tzinfo=SEOUL),
            deadline_source_text="9월 20일까지",
        )
        if status == "processed"
        else None
    )
    return Notice(
        workspace_id=WORKSPACE_ID,
        channel_id=CHANNEL_ID,
        message_ts=MESSAGE_TS,
        text="외부에 보여 주면 안 되는 원문",
        original_url="https://forms.example.test/task",
        canonical_url="https://forms.example.test/task",
        source_permalink=PERMALINK,
        posted_at=datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        processing_status=status,
        analysis=analysis,
        last_error="실패" if status == "processing_failed" else None,
    )


def test_command_acks_with_empty_guide_when_no_notices() -> None:
    ack = RecordingAck()
    handler = create_recent_notices_command_handler(NoticeService({CHANNEL_ID}))

    handler(ack, COMMAND, logging.getLogger("test"))

    assert ack.calls[0][0][0] == "<@U0000000001> 아직 처리된 공지가 없습니다."


def test_message_handler_fetches_permalink_only_for_accepted_messages() -> None:
    service = NoticeService({CHANNEL_ID})
    permalink_provider = FakePermalinkProvider()
    handler = create_message_event_handler(service, permalink_provider)

    handler(
        channel_message(),
        {"team_id": WORKSPACE_ID},
        {"bot_user_id": "USEULSEUL", "bot_id": "BSEULSEUL"},
        logging.getLogger("test"),
    )
    handler(
        channel_message(channel="CNOTALLOWED"),
        {"team_id": WORKSPACE_ID},
        {"bot_user_id": "USEULSEUL", "bot_id": "BSEULSEUL"},
        logging.getLogger("test"),
    )

    assert permalink_provider.calls == [(CHANNEL_ID, MESSAGE_TS)]
    assert service.recent_notices(5)[0].source_permalink == PERMALINK


def test_message_handler_skips_notice_when_permalink_lookup_fails() -> None:
    service = NoticeService({CHANNEL_ID})
    handler = create_message_event_handler(service, FailingPermalinkProvider())

    handler(
        channel_message(),
        {"team_id": WORKSPACE_ID},
        {"bot_user_id": "USEULSEUL", "bot_id": "BSEULSEUL"},
        logging.getLogger("test"),
    )

    assert service.recent_notices(5) == []


def test_slack_web_client_returns_message_permalink() -> None:
    client = FakeSlackWebClient({"permalink": PERMALINK})

    result = SlackWebApiClient(client).get_message_permalink(CHANNEL_ID, MESSAGE_TS)

    assert result == PERMALINK
    assert client.calls == [{"channel": CHANNEL_ID, "message_ts": MESSAGE_TS}]


def test_slack_web_client_rejects_response_without_permalink() -> None:
    client = FakeSlackWebClient({})

    try:
        SlackWebApiClient(client).get_message_permalink(CHANNEL_ID, MESSAGE_TS)
    except MessagePermalinkError as error:
        assert str(error) == "Slack 응답에 원문 링크가 없습니다."
    else:
        raise AssertionError("MessagePermalinkError가 발생해야 합니다.")


def test_view_never_exposes_original_message_text() -> None:
    text = build_recent_notices_text("U0000000001", [notice()])

    assert "외부에 보여 주면 안 되는 원문" not in text
    assert "폼으로 과제를 제출합니다." in text
    assert f"<{PERMALINK}|Slack 원문 보기>" in text


def test_failed_analysis_shows_only_status_and_source_link() -> None:
    text = build_recent_notices_text("U0000000001", [notice("processing_failed")])

    assert "AI 분석 실패" in text
    assert "외부에 보여 주면 안 되는 원문" not in text
    assert PERMALINK in text


def test_any_slash_command_matches_developer_specific_names() -> None:
    assert ANY_SLASH_COMMAND.match("/seulseul")
    assert ANY_SLASH_COMMAND.match("/seulseul-kgj")
    assert not ANY_SLASH_COMMAND.match("seulseul")


def test_register_handlers_accepts_socket_mode_app() -> None:
    app = App(token="xoxb-test-token", token_verification_enabled=False)

    register_handlers(app, NoticeService({CHANNEL_ID}))
