"""Slack 핸들러와 사용자 응답을 실제 Slack API 없이 검증한다."""

import logging
from datetime import datetime
from typing import Any
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from slack_bolt import App

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.model import Notice, ProcessingStatus
from seulseul.notices.service import NoticeService
from seulseul.slack.client import (
    MessagePermalinkError,
    SlackCommandResponder,
    SlackWebApiClient,
    UserProfileError,
)
from seulseul.slack.handlers import (
    SEULSEUL_COMMAND,
    create_message_event_handler,
    create_seulseul_command_handler,
    register_handlers,
)
from seulseul.slack.views import build_recent_notices_text
from seulseul.users.repository import InMemoryStudentRepository
from seulseul.users.service import StudentService

CHANNEL_ID = "C0000000001"
WORKSPACE_ID = "T0000000001"
MESSAGE_TS = "1789344000.000100"
PERMALINK = "https://workspace.slack.com/archives/C0000000001/p1789344000000100"
USER_ID = "U0000000001"
COMMAND = {
    "command": SEULSEUL_COMMAND,
    "text": "",
    "team_id": WORKSPACE_ID,
    "user_id": USER_ID,
    "channel_id": "D1",
}
SEOUL = ZoneInfo("Asia/Seoul")


class RecordingAck:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class FakeUserProfileProvider:
    def __init__(self, display_name: str) -> None:
        self.display_name = display_name
        self.calls: list[str] = []

    def get_display_name(self, user_id: str) -> str:
        self.calls.append(user_id)
        return self.display_name


class FailingUserProfileProvider:
    def get_display_name(self, user_id: str) -> str:
        raise UserProfileError("조회 실패")


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
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.permalink_calls: list[dict[str, str]] = []
        self.user_calls: list[dict[str, str]] = []

    def chat_getPermalink(self, **kwargs: str) -> dict[str, Any]:
        self.permalink_calls.append(kwargs)
        return self.response

    def users_info(self, **kwargs: str) -> dict[str, Any]:
        self.user_calls.append(kwargs)
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


def create_command_handler(
    display_name: str = "4기_광주_3반_홍길동",
) -> tuple[Any, InMemoryStudentRepository]:
    repository = InMemoryStudentRepository()
    handler = create_seulseul_command_handler(
        NoticeService({CHANNEL_ID}),
        StudentService(repository),
        FakeUserProfileProvider(display_name),
    )
    return handler, repository


def test_command_responder_sends_only_to_command_user() -> None:
    respond = RecordingAck()
    responder = SlackCommandResponder(respond)

    assert respond.calls == []
    responder.send("가입이 완료되었습니다.")

    assert respond.calls == [(("가입이 완료되었습니다.",), {"response_type": "ephemeral"})]


@pytest.mark.parametrize(
    ("action", "display_name", "workspace_id", "expected_text"),
    [
        ("시작", "4기_광주_3반_홍길동", WORKSPACE_ID, "가입이 완료"),
        ("시작", "광주_3반_홍길동", WORKSPACE_ID, "표시 이름"),
        ("해지", "4기_광주_3반_홍길동", WORKSPACE_ID, "가입된 정보가 없습니다"),
        ("", "4기_광주_3반_홍길동", WORKSPACE_ID, "처리된 공지가 없습니다"),
        ("공지", "4기_광주_3반_홍길동", WORKSPACE_ID, "처리된 공지가 없습니다"),
        ("도움말", "4기_광주_3반_홍길동", WORKSPACE_ID, "/seulseul 시작"),
        ("시작", "4기_광주_3반_홍길동", "", "워크스페이스 또는 사용자"),
    ],
)
def test_command_routes_delegate_responses_to_client(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    display_name: str,
    workspace_id: str,
    expected_text: str,
) -> None:
    send = Mock()
    monkeypatch.setattr(SlackCommandResponder, "send", send)
    respond = Mock(side_effect=AssertionError("핸들러에서 respond를 직접 호출하면 안 됩니다."))
    handler, _ = create_command_handler(display_name)

    handler(
        RecordingAck(),
        respond,
        {**COMMAND, "text": action, "team_id": workspace_id},
        logging.getLogger("test"),
    )

    send.assert_called_once()
    assert expected_text in send.call_args.args[0]
    respond.assert_not_called()


def test_start_command_acknowledges_before_profile_lookup_and_response() -> None:
    steps: list[str] = []
    repository = InMemoryStudentRepository()

    def get_display_name(user_id: str) -> str:
        assert steps == ["ack"]
        steps.append("profile")
        return "4기_광주_3반_홍길동"

    def respond(text: str, **kwargs: Any) -> None:
        assert repository.get(WORKSPACE_ID, USER_ID) is not None
        steps.append("response")

    handler = create_seulseul_command_handler(
        NoticeService({CHANNEL_ID}),
        StudentService(repository),
        Mock(get_display_name=get_display_name),
    )

    handler(
        lambda: steps.append("ack"),
        respond,
        {**COMMAND, "text": "시작"},
        logging.getLogger("test"),
    )

    assert steps == ["ack", "profile", "response"]


def test_command_acks_with_empty_guide_when_no_notices() -> None:
    ack = RecordingAck()
    respond = RecordingAck()
    handler, _ = create_command_handler()

    handler(ack, respond, COMMAND, logging.getLogger("test"))

    assert ack.calls == [((), {})]
    assert respond.calls[0][0][0] == f"<@{USER_ID}> 아직 처리된 공지가 없습니다."


def test_start_command_enrolls_student_after_profile_lookup() -> None:
    ack = RecordingAck()
    respond = RecordingAck()
    handler, repository = create_command_handler()

    handler(ack, respond, {**COMMAND, "text": "시작"}, logging.getLogger("test"))

    student = repository.get(WORKSPACE_ID, USER_ID)
    assert ack.calls == [((), {})]
    assert student is not None
    assert student.class_number == 3
    assert "가입이 완료" in respond.calls[0][0][0]


def test_start_command_guides_invalid_display_name() -> None:
    respond = RecordingAck()
    handler, repository = create_command_handler("광주_3반_홍길동")

    handler(
        RecordingAck(),
        respond,
        {**COMMAND, "text": "시작"},
        logging.getLogger("test"),
    )

    assert repository.get(WORKSPACE_ID, USER_ID) is None
    assert "4기_광주_<1~4>반_<이름>" in respond.calls[0][0][0]


def test_start_command_guides_profile_lookup_failure() -> None:
    respond = RecordingAck()
    handler = create_seulseul_command_handler(
        NoticeService({CHANNEL_ID}),
        StudentService(InMemoryStudentRepository()),
        FailingUserProfileProvider(),
    )

    handler(
        RecordingAck(),
        respond,
        {**COMMAND, "text": "시작"},
        logging.getLogger("test"),
    )

    assert "users:read" in respond.calls[0][0][0]


def test_withdraw_command_deletes_student() -> None:
    respond = RecordingAck()
    handler, repository = create_command_handler()
    handler(
        RecordingAck(),
        RecordingAck(),
        {**COMMAND, "text": "시작"},
        logging.getLogger("test"),
    )

    handler(
        RecordingAck(),
        respond,
        {**COMMAND, "text": "해지"},
        logging.getLogger("test"),
    )

    assert repository.get(WORKSPACE_ID, USER_ID) is None
    assert "개인 체크리스트가 삭제" in respond.calls[0][0][0]


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
    assert client.permalink_calls == [{"channel": CHANNEL_ID, "message_ts": MESSAGE_TS}]


def test_slack_web_client_rejects_response_without_permalink() -> None:
    client = FakeSlackWebClient({})

    try:
        SlackWebApiClient(client).get_message_permalink(CHANNEL_ID, MESSAGE_TS)
    except MessagePermalinkError as error:
        assert str(error) == "Slack 응답에 원문 링크가 없습니다."
    else:
        raise AssertionError("MessagePermalinkError가 발생해야 합니다.")


def test_slack_web_client_returns_profile_display_name() -> None:
    client = FakeSlackWebClient({"user": {"profile": {"display_name": "4기_광주_2반_홍길동"}}})

    display_name = SlackWebApiClient(client).get_display_name(USER_ID)

    assert display_name == "4기_광주_2반_홍길동"
    assert client.user_calls == [{"user": USER_ID}]


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


def test_slash_command_name_is_fixed() -> None:
    assert SEULSEUL_COMMAND == "/seulseul"


def test_register_handlers_accepts_socket_mode_app() -> None:
    app = App(token="xoxb-test-token", token_verification_enabled=False)

    register_handlers(
        app,
        NoticeService({CHANNEL_ID}),
        StudentService(InMemoryStudentRepository()),
    )
