"""Slack 핸들러와 사용자 응답을 실제 Slack API 없이 검증한다."""

import json
import logging
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from slack_bolt import App
from slack_sdk.errors import SlackApiError
from slack_sdk.web import SlackResponse

from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistItem,
    DailyChecklistBoard,
)
from seulseul.notices.service import NoticeService
from seulseul.slack.client import (
    MessagePermalinkError,
    SlackChecklistClient,
    SlackCommandResponder,
    SlackWebApiClient,
    UserProfileError,
)
from seulseul.slack.handlers import (
    SEULSEUL_COMMAND,
    create_checklist_action_handler,
    create_message_event_handler,
    create_seulseul_command_handler,
    register_checklist_handlers,
    register_handlers,
)
from seulseul.slack.views import build_daily_checklist_message
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


class RecordingAck:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class FakeUserProfileProvider:
    def __init__(self, real_name: str) -> None:
        self.real_name = real_name
        self.calls: list[str] = []

    def get_real_name(self, user_id: str) -> str:
        self.calls.append(user_id)
        return self.real_name


class FailingUserProfileProvider:
    def get_real_name(self, user_id: str) -> str:
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


def create_command_handler(
    real_name: str = "4기_광주_3반_홍길동",
) -> tuple[Any, InMemoryStudentRepository]:
    repository = InMemoryStudentRepository()
    handler = create_seulseul_command_handler(
        StudentService(repository),
        FakeUserProfileProvider(real_name),
    )
    return handler, repository


def test_command_responder_sends_only_to_command_user() -> None:
    respond = RecordingAck()
    responder = SlackCommandResponder(respond)

    assert respond.calls == []
    responder.send("가입이 완료되었습니다.")

    assert respond.calls == [(("가입이 완료되었습니다.",), {"response_type": "ephemeral"})]


@pytest.mark.parametrize(
    ("action", "real_name", "workspace_id", "expected_text"),
    [
        ("시작", "4기_광주_3반_홍길동", WORKSPACE_ID, "가입이 완료"),
        ("시작", "광주_3반_홍길동", WORKSPACE_ID, "성명"),
        ("해지", "4기_광주_3반_홍길동", WORKSPACE_ID, "가입된 정보가 없습니다"),
        ("", "4기_광주_3반_홍길동", WORKSPACE_ID, "/seulseul 시작"),
        ("공지", "4기_광주_3반_홍길동", WORKSPACE_ID, "/seulseul 시작"),
        ("체크리스트", "4기_광주_3반_홍길동", WORKSPACE_ID, "/seulseul 시작"),
        ("도움말", "4기_광주_3반_홍길동", WORKSPACE_ID, "/seulseul 시작"),
        ("시작", "4기_광주_3반_홍길동", "", "워크스페이스 또는 사용자"),
    ],
)
def test_command_routes_delegate_responses_to_client(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    real_name: str,
    workspace_id: str,
    expected_text: str,
) -> None:
    send = Mock()
    monkeypatch.setattr(SlackCommandResponder, "send", send)
    respond = Mock(side_effect=AssertionError("핸들러에서 respond를 직접 호출하면 안 됩니다."))
    handler, _ = create_command_handler(real_name)

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

    def get_real_name(user_id: str) -> str:
        assert steps == ["ack"]
        steps.append("profile")
        return "4기_광주_3반_홍길동"

    def respond(text: str, **kwargs: Any) -> None:
        assert repository.get(WORKSPACE_ID, USER_ID) is not None
        steps.append("response")

    handler = create_seulseul_command_handler(
        StudentService(repository),
        Mock(get_real_name=get_real_name),
    )

    handler(
        lambda: steps.append("ack"),
        respond,
        {**COMMAND, "text": "시작"},
        logging.getLogger("test"),
    )

    assert steps == ["ack", "profile", "response"]


def test_bare_command_acks_and_shows_only_start_withdraw_and_dm_guide() -> None:
    ack = RecordingAck()
    respond = RecordingAck()
    handler, _ = create_command_handler()

    handler(ack, respond, COMMAND, logging.getLogger("test"))

    assert ack.calls == [((), {})]
    assert "/seulseul 시작" in respond.calls[0][0][0]
    assert "/seulseul 해지" in respond.calls[0][0][0]
    assert "/seulseul 공지" not in respond.calls[0][0][0]


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


def test_start_command_guides_invalid_real_name() -> None:
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
    assert "Slack 성명" in respond.calls[0][0][0]


def test_start_command_guides_profile_lookup_failure() -> None:
    respond = RecordingAck()
    handler = create_seulseul_command_handler(
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


def test_slack_web_client_returns_profile_real_name() -> None:
    client = FakeSlackWebClient(
        {"user": {"profile": {"real_name": " 4기_광주_2반_홍길동 ", "display_name": "길동"}}}
    )

    real_name = SlackWebApiClient(client).get_real_name(USER_ID)

    assert real_name == "4기_광주_2반_홍길동"
    assert client.user_calls == [{"user": USER_ID}]


@pytest.mark.parametrize("real_name", [None, "", "   ", 123])
def test_slack_web_client_rejects_missing_real_name_even_with_valid_display_name(
    real_name: Any,
) -> None:
    client = FakeSlackWebClient(
        {"user": {"profile": {"real_name": real_name, "display_name": "4기_광주_2반_홍길동"}}}
    )

    with pytest.raises(UserProfileError, match="성명이 설정되어 있지 않습니다"):
        SlackWebApiClient(client).get_real_name(USER_ID)


@pytest.mark.parametrize(
    ("real_name", "display_name", "expected_class"),
    [
        ("4기_광주_2반_홍길동", "길동", 2),
        ("4기_광주_2반_홍길동", "4기_광주_4반_홍길동", 2),
        ("홍길동", "4기_광주_4반_홍길동", None),
        ("", "4기_광주_4반_홍길동", None),
    ],
)
def test_start_command_enrolls_using_real_name_only(
    real_name: str, display_name: str, expected_class: int | None
) -> None:
    repository = InMemoryStudentRepository()
    client = FakeSlackWebClient(
        {"user": {"profile": {"real_name": real_name, "display_name": display_name}}}
    )
    handler = create_seulseul_command_handler(StudentService(repository), SlackWebApiClient(client))
    respond = RecordingAck()

    handler(RecordingAck(), respond, {**COMMAND, "text": "시작"}, logging.getLogger("test"))

    student = repository.get(WORKSPACE_ID, USER_ID)
    if expected_class is None:
        assert student is None
        assert "성명" in respond.calls[0][0][0]
    else:
        assert student is not None
        assert student.class_number == expected_class
        assert student.real_name == real_name
        assert "가입이 완료" in respond.calls[0][0][0]


def test_slash_command_name_is_fixed() -> None:
    assert SEULSEUL_COMMAND == "/seulseul"


def test_register_handlers_accepts_socket_mode_app() -> None:
    app = App(token="xoxb-test-token", token_verification_enabled=False)

    register_handlers(
        app,
        NoticeService({CHANNEL_ID}),
        StudentService(InMemoryStudentRepository()),
    )


def daily_board():
    return DailyChecklistBoard(
        id=uuid4(),
        message_date=date(2026, 9, 16),
        items=(
            ChecklistItem(
                uuid4(),
                "과제 <@everyone>",
                "요약",
                datetime(2026, 9, 20, tzinfo=timezone.utc),
                "https://forms.example.test/task",
                PERMALINK,
                False,
            ),
        ),
        pending_count=1,
        completed_count=0,
        show_completed=False,
        page=0,
        page_count=1,
        refreshed_at=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )


def test_daily_view_has_links_explicit_buttons_and_plain_text_summary() -> None:
    board = daily_board()
    payload = build_daily_checklist_message(board)
    section = next(block for block in payload["blocks"] if block.get("accessory"))
    assert section["text"]["type"] == "plain_text"
    assert section["accessory"]["action_id"] == "checklist_complete"
    value = json.loads(section["accessory"]["value"])
    assert value == {"daily": str(board.id), "item": str(board.items[0].id)}
    assert PERMALINK in str(payload)
    assert "https://forms.example.test/task" in str(payload)
    assert len(payload["blocks"]) < 50
    completed = replace(
        board,
        items=(replace(board.items[0], completed=True),),
        show_completed=True,
        pending_count=0,
        completed_count=1,
    )
    assert "checklist_undo" in str(build_daily_checklist_message(completed))


def test_daily_client_posts_regular_dm_and_updates_by_saved_address() -> None:
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    client.chat_postMessage.return_value = {"ts": "123.456"}
    adapter = SlackChecklistClient(client)
    board = daily_board()

    assert adapter.send(USER_ID, board) == ("DTEST", "123.456")
    client.conversations_open.assert_called_once_with(users=USER_ID)
    args = client.chat_postMessage.call_args.kwargs
    assert args["channel"] == "DTEST" and "response_type" not in args
    assert args["metadata"]["event_payload"]["delivery_id"] == str(board.id)
    adapter.update("DTEST", "123.456", board)
    assert client.chat_update.call_args.kwargs["ts"] == "123.456"
    assert client.chat_update.call_args.kwargs["channel"] == "DTEST"
    client.chat_postEphemeral.assert_not_called()


@pytest.mark.parametrize(
    ("stage", "uncertain"), [("conversations_open", False), ("chat_postMessage", True)]
)
def test_daily_client_classifies_connection_failure(stage, uncertain):
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    getattr(client, stage).side_effect = OSError("secret transport detail")
    with pytest.raises(ChecklistDeliveryError) as caught:
        SlackChecklistClient(client).send(USER_ID, daily_board())
    assert caught.value.uncertain is uncertain
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "code", "uncertain"),
    [
        (429, "ratelimited", False),
        (200, "missing_scope", False),
        (500, "internal_error", True),
        (200, "request_timeout", True),
    ],
)
def test_daily_client_classifies_slack_errors(status, code, uncertain):
    response = SlackResponse(
        client=Mock(),
        http_verb="POST",
        api_url="https://slack.com/api/test",
        req_args={},
        data={"ok": False, "error": code},
        headers={"Retry-After": "2"},
        status_code=status,
    )
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    client.chat_postMessage.side_effect = SlackApiError("secret detail", response)
    with pytest.raises(ChecklistDeliveryError) as caught:
        SlackChecklistClient(client).send(USER_ID, daily_board())
    assert caught.value.code == code
    assert caught.value.uncertain is uncertain
    assert caught.value.retry_after == 2
    assert "secret" not in str(caught.value)


def test_daily_client_disables_sdk_transport_retries_without_making_requests() -> None:
    adapter = SlackChecklistClient.from_token("xoxb-fake")
    assert adapter._client.retry_handlers == []
    assert adapter._client.timeout == 10


def action_body():
    return {
        "team": {"id": WORKSPACE_ID},
        "user": {"id": USER_ID},
        "container": {"channel_id": "DTEST", "message_ts": "123.456"},
        "actions": [{"action_id": "checklist_complete", "value": "{}"}],
    }


def test_checklist_handler_acknowledges_before_delegating():
    calls = []
    service = Mock()

    def handle(*args):
        assert calls == ["ack"]
        calls.append("service")
        return True

    service.handle_action.side_effect = handle
    handler = create_checklist_action_handler(service)
    respond = Mock()
    handler(lambda: calls.append("ack"), respond, action_body(), logging.getLogger("test"))
    assert calls == ["ack", "service"]
    respond.assert_not_called()
    service.handle_action.assert_called_once_with(
        WORKSPACE_ID,
        USER_ID,
        "DTEST",
        "123.456",
        "complete",
        "{}",
    )


@pytest.mark.parametrize(
    "error",
    [ChecklistActionError("최신 메시지를 이용해 주세요."), RuntimeError("sensitive DB details")],
)
def test_checklist_handler_reports_errors_only_through_responder(error):
    service = Mock()
    service.handle_action.side_effect = error
    respond = RecordingAck()
    create_checklist_action_handler(service)(
        RecordingAck(), respond, action_body(), logging.getLogger("test")
    )
    assert respond.calls[0][1]["response_type"] == "ephemeral"
    assert "sensitive" not in str(respond.calls)


def test_checklist_action_listener_registers_without_live_api():
    app = App(token="xoxb-test-token", token_verification_enabled=False)
    register_checklist_handlers(app, Mock())
