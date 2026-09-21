"""Slack 핸들러와 사용자 응답을 실제 Slack API 없이 검증한다."""

import json
import logging
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timezone
from typing import Any
from unittest.mock import ANY, Mock
from uuid import uuid4

import pytest
from slack_bolt import App
from slack_sdk.errors import SlackApiError
from slack_sdk.models.blocks import Block
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
    create_user_change_handler,
    register_checklist_handlers,
    register_handlers,
)
from seulseul.slack.views import build_daily_checklist_message
from seulseul.users.repository import InMemoryStudentRepository
from seulseul.users.service import StudentService


def cleanup_client():
    client = Mock()
    client.auth_test.return_value = {"team_id": "TTEST", "user_id": "UBOT", "bot_id": "BBOT"}
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    return client


@pytest.mark.parametrize("deleted", [True, False])
def test_withdrawal_confirmation_is_normal_dm_not_ephemeral(deleted):
    client = cleanup_client()
    SlackChecklistClient(client).send_withdrawal("USTUDENT", deleted)
    client.conversations_open.assert_called_once_with(users="USTUDENT")
    client.chat_postEphemeral.assert_not_called()
    payload = client.chat_postMessage.call_args.kwargs
    assert payload["channel"] == "DTEST"
    assert "response_type" not in payload
    assert ("해지가 완료" if deleted else "가입되어 있지") in payload["text"]


def test_invalid_name_guidance_is_normal_dm_and_command_only_acks():
    client = cleanup_client()
    repository = InMemoryStudentRepository()
    service = StudentService(
        repository, on_invalid_name=SlackChecklistClient(client).send_enrollment_guidance
    )
    handler = create_seulseul_command_handler(service, Mock(get_real_name=lambda user: "불일치"))
    ack, respond = Mock(), Mock()
    handler(ack, respond, {**COMMAND, "text": "시작"}, logging.getLogger("test"))
    ack.assert_called_once_with()
    respond.assert_not_called()
    client.chat_postEphemeral.assert_not_called()
    client.chat_delete.assert_not_called()
    payload = client.chat_postMessage.call_args.kwargs
    assert payload["channel"] == "DTEST"
    assert "성명" in payload["text"] and "/seulseul 시작" in payload["text"]
    assert repository.get(WORKSPACE_ID, USER_ID) is None


@pytest.mark.parametrize("code", ["message_not_found", "ratelimited"])
def test_cleanup_ignores_already_deleted_but_propagates_rate_limit(code):
    client = cleanup_client()
    client.conversations_history.return_value = {
        "messages": [
            {"user": "UBOT", "ts": "100.1"},
            {"user": "UBOT", "ts": "100.2"},
        ]
    }
    response = SlackResponse(
        client=client,
        http_verb="POST",
        api_url="https://slack.com/api/chat.delete",
        req_args={},
        data={"ok": False, "error": code},
        headers={},
        status_code=200,
    )
    client.chat_delete.side_effect = [SlackApiError("failure", response), {"ok": True}]
    adapter = SlackChecklistClient(client)
    if code == "message_not_found":
        adapter.delete_previous_messages("TTEST", "USTUDENT")
        assert client.chat_delete.call_count == 2
    else:
        with pytest.raises(ChecklistDeliveryError):
            adapter.delete_previous_messages("TTEST", "USTUDENT")
        assert client.chat_delete.call_count == 1


def test_cleanup_paginates_and_only_deletes_own_dm_messages_including_replies():
    client = cleanup_client()
    client.conversations_history.side_effect = [
        {
            "messages": [
                {"user": "UBOT", "ts": "100.1"},
                {"user": "USTUDENT", "ts": "100.2", "reply_count": 2},
            ],
            "response_metadata": {"next_cursor": "next"},
        },
        {
            "messages": [
                {"bot_id": "BBOT", "ts": "99.1"},
                {"user": "UOTHERBOT", "bot_id": "BOTHER", "ts": "99.2"},
            ]
        },
    ]
    client.conversations_replies.return_value = {
        "messages": [
            {"user": "UBOT", "ts": "101.1"},
            {"user": "USTUDENT", "ts": "101.2"},
        ]
    }
    SlackChecklistClient(client).delete_previous_messages("TTEST", "USTUDENT")
    client.conversations_open.assert_called_once_with(users="USTUDENT")
    assert client.conversations_history.call_args_list[1].kwargs["cursor"] == "next"
    assert [call.kwargs for call in client.chat_delete.call_args_list] == [
        {"channel": "DTEST", "ts": ts} for ts in ["101.1", "100.1", "99.1"]
    ]


@pytest.mark.parametrize("case", ["workspace", "channel", "history"])
def test_cleanup_refuses_unsafe_scope_or_incomplete_history(case):
    client = cleanup_client()
    if case == "workspace":
        client.auth_test.return_value["team_id"] = "TOTHER"
    elif case == "channel":
        client.conversations_open.return_value = {"channel": {"id": "CCHANNEL"}}
    else:
        client.conversations_history.return_value = {"messages": [], "has_more": True}
    with pytest.raises(ChecklistDeliveryError):
        SlackChecklistClient(client).delete_previous_messages("TTEST", "USTUDENT")
    client.chat_delete.assert_not_called()


@pytest.mark.parametrize("action", ["시작", "해지"])
def test_cleanup_failure_is_acknowledged_without_success_message(action):
    @contextmanager
    def reset(workspace, user):
        raise ChecklistDeliveryError("missing_scope")
        yield

    repository = InMemoryStudentRepository()
    handler = create_seulseul_command_handler(
        StudentService(repository, reset_messages=reset),
        FakeUserProfileProvider("4기_광주_3반_가상학생"),
    )
    ack, respond = Mock(), Mock()
    handler(
        ack,
        respond,
        {"text": action, "team_id": "TTEST", "user_id": "UTEST"},
        logging.getLogger(__name__),
    )
    ack.assert_called_once()
    respond.assert_not_called()


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


def test_user_change_uses_current_real_name_not_event_payload():
    repository = InMemoryStudentRepository()
    service = StudentService(repository)
    service.enroll(WORKSPACE_ID, USER_ID, "4기_광주_3반_가상학생")
    provider = FakeUserProfileProvider("4기_광주_1반_가상학생")
    handler = create_user_change_handler(service, provider)
    handler(
        {
            "type": "user_change",
            "user": {"id": USER_ID, "profile": {"real_name": "4기_광주_2반_과거"}},
        },
        {"team_id": WORKSPACE_ID},
        {"team_id": WORKSPACE_ID},
        logging.getLogger("test"),
    )
    assert repository.get(WORKSPACE_ID, USER_ID).class_number == 1
    assert provider.calls == [USER_ID]


@pytest.mark.parametrize(
    "user,body,context",
    [
        (None, {"team_id": WORKSPACE_ID}, {}),
        ({"id": "bad"}, {"team_id": WORKSPACE_ID}, {}),
        ({"id": USER_ID}, {}, {}),
        ({"id": USER_ID}, {"team_id": WORKSPACE_ID}, {"team_id": "TOTHER"}),
    ],
)
def test_user_change_invalid_identifiers_do_not_call_service(user, body, context):
    service = Mock()
    create_user_change_handler(service, Mock())(
        {"type": "user_change", "user": user}, body, context, logging.getLogger("test")
    )
    service.sync_profile.assert_not_called()


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
    ("action", "real_name", "workspace_id"),
    [
        ("시작", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("시작", "광주_3반_홍길동", WORKSPACE_ID),
        ("해지", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("공지", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("체크리스트", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("도움말", "4기_광주_3반_홍길동", WORKSPACE_ID),
        ("시작", "4기_광주_3반_홍길동", ""),
    ],
)
def test_command_routes_never_send_responses(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    real_name: str,
    workspace_id: str,
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

    send.assert_not_called()
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

    assert steps == ["ack", "profile"]
    assert repository.get(WORKSPACE_ID, USER_ID) is not None


def test_bare_command_only_acks_without_message() -> None:
    ack = RecordingAck()
    respond = RecordingAck()
    handler, _ = create_command_handler()

    handler(ack, respond, COMMAND, logging.getLogger("test"))

    assert ack.calls == [((), {})]
    assert respond.calls == []


def test_unexpected_command_failure_logs_type_without_responding_or_leaking_details(caplog):
    service = Mock()
    service.withdraw.side_effect = RuntimeError("sensitive token or user content")
    handler = create_seulseul_command_handler(service, Mock())
    ack, respond = Mock(), Mock()
    handler(ack, respond, {**COMMAND, "text": "해지"}, logging.getLogger("test"))
    ack.assert_called_once_with()
    respond.assert_not_called()
    assert "RuntimeError" in caplog.text
    assert "sensitive" not in caplog.text


def test_start_command_enrolls_student_after_profile_lookup() -> None:
    ack = RecordingAck()
    respond = RecordingAck()
    handler, repository = create_command_handler()

    handler(ack, respond, {**COMMAND, "text": "시작"}, logging.getLogger("test"))

    student = repository.get(WORKSPACE_ID, USER_ID)
    assert ack.calls == [((), {})]
    assert student is not None
    assert student.class_number == 3
    assert respond.calls == []


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
    assert respond.calls == []


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

    assert respond.calls == []


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
    assert respond.calls == []


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


def test_message_handler_records_failure_when_permalink_lookup_fails() -> None:
    service = NoticeService({CHANNEL_ID})
    handler = create_message_event_handler(service, FailingPermalinkProvider())

    handler(
        channel_message(),
        {"team_id": WORKSPACE_ID},
        {"bot_user_id": "USEULSEUL", "bot_id": "BSEULSEUL"},
        logging.getLogger("test"),
    )

    notices = service.failed_notices(5)
    assert len(notices) == 1
    assert notices[0].source_permalink == ""
    assert notices[0].analysis is None
    assert "원문 링크 조회 실패" in notices[0].last_error


def edited_event(text, **overrides):
    payload = {
        "type": "message",
        "subtype": "message_changed",
        "channel": CHANNEL_ID,
        "event_ts": "1789344200.000100",
        "ts": "1789344200.000100",
        "message": {"ts": MESSAGE_TS, "user": "UWRITER", "text": text},
    }
    payload.update(overrides)
    return payload


def dispatch_message(handler, event, workspace=WORKSPACE_ID):
    handler(
        event,
        {"team_id": workspace},
        {"bot_user_id": "USEULSEUL", "bot_id": "BSEULSEUL"},
        logging.getLogger("test"),
    )


def test_edit_before_create_looks_up_permalink_for_original_message_not_event():
    service = NoticeService({CHANNEL_ID})
    provider = FakePermalinkProvider()
    handler = create_message_event_handler(service, provider)
    dispatch_message(handler, edited_event("수정 공지 https://forms.example.test/task"))
    assert provider.calls == [(CHANNEL_ID, MESSAGE_TS)]
    assert service.recent_notices(5)[0].message_ts == MESSAGE_TS


@pytest.mark.parametrize("action", ["edit", "remove_links", "delete"])
def test_source_changes_use_stored_permalink_and_delete_never_calls_slack(action):
    service = NoticeService({CHANNEL_ID})
    dispatch_message(
        create_message_event_handler(service, FakePermalinkProvider()), channel_message()
    )
    provider = Mock()
    provider.get_message_permalink.side_effect = AssertionError("Slack 호출이 필요하지 않습니다")
    handler = create_message_event_handler(service, provider)
    payload = edited_event("수정 https://forms.example.test/task" if action == "edit" else "")
    if action == "delete":
        payload = {
            "type": "message",
            "subtype": "message_deleted",
            "channel": CHANNEL_ID,
            "event_ts": "1789344200.000100",
            "deleted_ts": MESSAGE_TS,
        }
    dispatch_message(handler, payload)
    provider.get_message_permalink.assert_not_called()
    remaining = service.recent_notices(5)
    assert len(remaining) == (1 if action == "edit" else 0)
    if remaining:
        assert remaining[0].text.startswith("수정")


@pytest.mark.parametrize(
    "payload",
    [
        edited_event("링크 https://forms.example.test/task", channel="COUTSIDE"),
        edited_event("링크 https://forms.example.test/task", message={"ts": MESSAGE_TS}),
        edited_event("링크 https://forms.example.test/task", message=None),
        edited_event(
            "링크 https://forms.example.test/task",
            message={
                "ts": MESSAGE_TS,
                "thread_ts": "1.000001",
                "text": "https://forms.example.test/task",
            },
        ),
    ],
)
def test_invalid_mutation_never_calls_provider_or_removes_existing_notice(payload):
    service = NoticeService({CHANNEL_ID})
    dispatch_message(
        create_message_event_handler(service, FakePermalinkProvider()), channel_message()
    )
    provider = Mock()
    dispatch_message(create_message_event_handler(service, provider), payload)
    assert len(service.recent_notices(5)) == 1
    provider.get_message_permalink.assert_not_called()


def test_event_without_workspace_never_looks_up_permalink():
    provider = Mock()
    dispatch_message(
        create_message_event_handler(NoticeService({CHANNEL_ID}), provider),
        channel_message(),
        workspace="",
    )
    provider.get_message_permalink.assert_not_called()


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
        assert respond.calls == []
    else:
        assert student is not None
        assert student.class_number == expected_class
        assert student.real_name == real_name
        assert respond.calls == []


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


def test_daily_view_has_links_explicit_icon_buttons_and_no_summary() -> None:
    board = daily_board()
    payload = build_daily_checklist_message(board)
    section = next(
        block for block in payload["blocks"][0]["child_blocks"] if block.get("accessory")
    )
    assert section["text"]["type"] == "mrkdwn"
    assert board.items[0].summary not in str(payload)
    assert section["accessory"]["text"]["text"] == "✓"
    assert "완료로 표시" in section["accessory"]["accessibility_label"]
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


@pytest.mark.parametrize("completed", [False, True])
def test_daily_view_groups_bold_titles_and_secondary_metadata_in_card(completed):
    board = daily_board()
    board = replace(
        board,
        items=(replace(board.items[0], completed=completed),),
        show_completed=completed,
        pending_count=int(not completed),
        completed_count=int(completed),
    )
    blocks = build_daily_checklist_message(board)["blocks"]
    card = blocks[0]
    assert card["type"] == "container"
    assert card["title"] == {"type": "plain_text", "text": "내 체크리스트"}
    assert card["subtitle"]["text"] == (
        ("완료 목록: 1개" if completed else "미완료 목록: 1개") + " · 마지막 갱신 09/16(수)"
    )
    assert card["width"] == "full" and card["has_header_divider"]
    assert blocks[1]["type"] == "actions"
    assert [button["action_id"] for button in blocks[1]["elements"]] == [
        "checklist_pending" if completed else "checklist_completed",
        "checklist_refresh",
    ]
    body, metadata = card["child_blocks"]
    assert body["text"] == {
        "type": "mrkdwn",
        "text": f"📝 *<{board.items[0].original_url}|과제 &lt;@everyone&gt;>*",
    }
    assert metadata == {
        "type": "context",
        "elements": [
            {"type": "plain_text", "text": "forms.example.test"},
            {"type": "mrkdwn", "text": f"· 09/20 09:00 마감 · <{PERMALINK}|원문>"},
        ],
    }
    assert body["accessory"]["action_id"] == (
        "checklist_undo" if completed else "checklist_complete"
    )
    assert "[완료]" not in str(blocks) and "[미완료]" not in str(blocks)
    assert "마감 전 항목" not in str(blocks)
    assert "제출 링크" not in str(blocks)
    assert body["accessory"]["text"]["text"] == ("↶" if completed else "✓")
    assert (
        "완료 취소" in body["accessory"]["accessibility_label"]
        if completed
        else "완료로 표시" in body["accessory"]["accessibility_label"]
    )
    assert "요약" not in str(blocks) and "09월 16일" not in str(blocks)
    assert "제출 후" not in str(blocks)
    assert "마지막 갱신" not in str(blocks[1:])
    assert "1/1" not in str(blocks[-1])
    assert "📅" not in str(blocks)


def test_daily_view_shortens_titles_and_escapes_mentions_without_showing_summary():
    board = daily_board()
    title = "<!channel> *제목*\n> <https://example.test|링크> & " * 20
    summary = "<!here> *요약*\n> 본문 " * 100
    items = tuple(
        replace(board.items[0], id=uuid4(), title=title, summary=summary) for _ in range(5)
    )
    blocks = build_daily_checklist_message(replace(board, items=items, pending_count=5))["blocks"]
    rows = blocks[0]["child_blocks"]
    assert len(rows) == 10 and len(blocks) < 20
    # SDK가 아직 container를 파싱하지 못하므로 자식은 SDK로, 컨테이너는 위 계약 테스트로 검증한다.
    for block in [*rows, *blocks[1:]]:
        parsed = Block.parse(block)
        assert parsed is not None
        parsed.to_dict()
        if block.get("accessory"):
            assert block["text"]["type"] == "mrkdwn"
            assert "&lt;!channel&gt;" in block["text"]["text"]
            assert "<!channel>" not in block["text"]["text"]
            assert block["text"]["text"].endswith("…>*")
            assert "\n" not in block["text"]["text"]
    assert "요약" not in str(blocks)
    assert items[0].title == title and items[0].summary == summary


def test_daily_view_links_each_title_to_its_own_submission_and_preserves_url():
    board = daily_board()
    urls = (
        "https://forms.example.test/task-one?name=A%20B&choice=1%7C2#step-2",
        "https://docs.example.test/task-two?answer=%3Cyes%3E&lang=ko",
    )
    items = tuple(
        replace(board.items[0], id=uuid4(), title=f"과제 {i}", original_url=url)
        for i, url in enumerate(urls, start=1)
    )
    blocks = build_daily_checklist_message(replace(board, items=items, pending_count=2))["blocks"]
    title_blocks = [block for block in blocks[0]["child_blocks"] if block.get("accessory")]
    assert len(title_blocks) == len(items)
    for block, item in zip(title_blocks, items, strict=True):
        assert f"<{item.original_url.replace('&', '&amp;')}|{item.title}>" in block["text"]["text"]
    assert all("제출 링크" not in str(block) for block in blocks)


def test_empty_daily_view_keeps_empty_state_and_shows_last_updated_in_seoul():
    board = replace(daily_board(), items=(), pending_count=0)
    payload = build_daily_checklist_message(board)
    assert "마지막 갱신 09/16(수)" in str(payload)
    assert "마감 전 항목" not in str(payload)
    assert "현재 남은 할 일이 없어요." in str(payload)
    card = payload["blocks"][0]
    assert card["type"] == "container"
    assert len(card["child_blocks"]) == 1
    assert card["child_blocks"][0]["type"] == "section"


@pytest.mark.parametrize("use_container", [True, False])
@pytest.mark.parametrize(
    "day,weekday",
    [(14, "월"), (15, "화"), (16, "수"), (17, "목"), (18, "금"), (19, "토"), (20, "일")],
)
def test_updated_date_uses_korean_weekday_and_seoul_date(use_container, day, weekday):
    board = replace(
        daily_board(),
        pending_count=4,
        refreshed_at=datetime(2026, 9, day - 1, 15, 30, tzinfo=timezone.utc),
        page_count=2,
    )
    payload = build_daily_checklist_message(board, use_container=use_container)
    blocks = payload["blocks"]
    hint = blocks[0]["subtitle"]["text"] if use_container else blocks[1]["elements"][0]["text"]
    assert hint == f"미완료 목록: 4개 · 마지막 갱신 09/{day}({weekday})"
    assert blocks[-1]["elements"] == [{"type": "plain_text", "text": "1/2"}]
    assert "제출 후" not in str(payload)
    assert "00:30" not in str(payload)


@pytest.mark.parametrize(
    ("show_completed", "pending", "completed"),
    [
        (True, 1, 1),
        (True, 2, 0),
        (True, 0, 1),
        (False, 1, 1),
        (False, 0, 1),
        (True, 0, 0),
        (False, 0, 0),
    ],
)
def test_daily_view_identifies_filter_and_points_to_hidden_pending_notices(
    show_completed, pending, completed
):
    board = replace(
        daily_board(),
        show_completed=show_completed,
        pending_count=pending,
        completed_count=completed,
    )
    if not (completed if show_completed else pending):
        board = replace(board, items=())
    elif show_completed:
        board = replace(board, items=(replace(board.items[0], completed=True),))
    payload = build_daily_checklist_message(board)
    hint = f"완료 목록: {completed}개" if show_completed else f"미완료 목록: {pending}개"
    hint += " · 마지막 갱신 09/16(수)"
    assert hint == payload["blocks"][0]["subtitle"]["text"]
    assert hint in payload["text"]
    assert all(block["type"] != "header" for block in payload["blocks"])
    buttons = payload["blocks"][1]["elements"]
    assert [button["action_id"] for button in buttons] == [
        "checklist_pending" if show_completed else "checklist_completed",
        "checklist_refresh",
    ]
    assert buttons[0]["text"] == {
        "type": "plain_text",
        "text": f"미완료 보기 ({pending})" if show_completed else f"완료 보기 ({completed})",
    }
    for button in buttons:
        assert "style" not in button
        assert "✓" not in button["text"]["text"]
        assert json.loads(button["value"]) == {"daily": str(board.id)}
    assert buttons[1]["text"]["text"] == "새로고침"


@pytest.mark.parametrize(
    ("url", "icon"),
    [
        ("https://forms.gle/task", "📝"),
        ("https://docs.google.com/forms/d/task", "📝"),
        ("https://form.naver.com/response/task", "📝"),
        ("https://docs.google.com/document/d/task", "📄"),
        ("https://example.test/FORMS/task", "📝"),
        ("https://example.test/%64ocs/task?form=1", "📄"),
        ("https://example.test/task?forms=1", "🔗"),
    ],
)
def test_daily_view_distinguishes_forms_from_documents_using_host_and_path(url, icon):
    board = daily_board()
    board = replace(board, items=(replace(board.items[0], original_url=url),))
    row = next(
        block
        for block in build_daily_checklist_message(board)["blocks"][0]["child_blocks"]
        if block.get("accessory")
    )
    assert row["text"]["text"].startswith(icon + " ")


@pytest.mark.parametrize("length", [27, 28, 29, 200])
def test_daily_view_title_limit_is_display_only(length):
    board = daily_board()
    title = "제" * length
    item = replace(board.items[0], title=title)
    row = next(
        block
        for block in build_daily_checklist_message(replace(board, items=(item,)))["blocks"][0][
            "child_blocks"
        ]
        if block.get("accessory")
    )
    label = title if length <= 28 else title[:27] + "…"
    assert f"|{label}>" in row["text"]["text"]
    assert len(label) <= 28 and item.title == title


def test_card_fallback_keeps_all_rows_controls_and_secondary_information():
    board = daily_board()
    card_payload = build_daily_checklist_message(board)
    fallback = build_daily_checklist_message(board, use_container=False)
    assert fallback["text"] == card_payload["text"]
    assert not any(block["type"] == "container" for block in fallback["blocks"])
    assert fallback["blocks"][2:-1] == card_payload["blocks"][0]["child_blocks"]
    assert fallback["blocks"][-1:] == card_payload["blocks"][1:]
    for block in fallback["blocks"]:
        parsed = Block.parse(block)
        assert parsed is not None
        parsed.to_dict()


def test_card_footer_only_shows_page_count_when_multiple_pages_exist():
    board = replace(daily_board(), page=1, page_count=3)
    blocks = build_daily_checklist_message(board)["blocks"]
    assert blocks[-1]["elements"][0]["text"] == "2/3"
    navigation = blocks[-2]["elements"]
    assert [button["action_id"] for button in navigation] == [
        "checklist_previous",
        "checklist_next",
    ]


@pytest.mark.parametrize("operation", ["send", "update"])
def test_daily_client_falls_back_once_only_when_card_blocks_are_explicitly_rejected(operation):
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    response = SlackResponse(
        client=Mock(),
        http_verb="POST",
        api_url="https://slack.com/api/test",
        req_args={},
        data={"ok": False, "error": "invalid_blocks"},
        headers={},
        status_code=200,
    )
    method = client.chat_postMessage if operation == "send" else client.chat_update
    method.side_effect = [
        SlackApiError("invalid blocks", response),
        {"ts": "123.456"},
        {"ts": "123.456"},
    ]
    adapter = SlackChecklistClient(client)
    board = daily_board()
    if operation == "send":
        assert adapter.send(USER_ID, board) == ("DTEST", "123.456")
    else:
        adapter.update("DTEST", "123.456", board)
    calls = method.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["blocks"][0]["type"] == "container"
    assert all(block["type"] != "container" for block in calls[1].kwargs["blocks"])
    assert calls[0].kwargs["channel"] == calls[1].kwargs["channel"] == "DTEST"
    client.chat_update.side_effect = None
    adapter.update("DTEST", "123.456", board)
    assert all(
        block["type"] != "container" for block in client.chat_update.call_args.kwargs["blocks"]
    )


@pytest.mark.parametrize("uncertain", [False, True])
def test_card_fallback_never_reposts_on_network_or_ambiguous_errors(uncertain):
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DTEST"}}
    client.chat_postMessage.side_effect = ChecklistDeliveryError(
        "invalid_blocks" if uncertain else "slack_connection_error", uncertain=uncertain
    )
    adapter = SlackChecklistClient(client)
    with pytest.raises(ChecklistDeliveryError):
        adapter.send(USER_ID, daily_board())
    client.chat_postMessage.assert_called_once()


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

    def handle(*args, trace_id):
        assert calls == ["ack"]
        assert len(trace_id) == 32
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
        trace_id=ANY,
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


def test_button_logs_share_trace_and_exclude_payload_and_exception_details(caplog):
    service = Mock()
    service.handle_action.side_effect = RuntimeError("secret-db-password")
    body = action_body()
    body["response_url"] = "https://hooks.example.test/secret-response-url"
    body["token"] = "secret-token"
    body["message"] = {"text": "private-notice-body"}
    body["actions"][0]["value"] = "private-button-value"
    caplog.set_level(logging.INFO)
    handler = create_checklist_action_handler(service)
    for _ in range(2):
        handler(RecordingAck(), RecordingAck(), body, logging.getLogger("test"))

    logs = [json.loads(record.getMessage()) for record in caplog.records]
    assert [entry["event"] for entry in logs] == [
        "checklist_action_received",
        "checklist_action_error",
        "checklist_action_received",
        "checklist_action_error",
    ]
    assert logs[0]["trace_id"] == logs[1]["trace_id"]
    assert logs[2]["trace_id"] == logs[3]["trace_id"]
    assert logs[0]["trace_id"] != logs[2]["trace_id"]
    assert len({entry["instance_id"] for entry in logs}) == 1
    assert logs[1]["error_type"] == "RuntimeError"
    assert logs[0]["operation"] == "complete"
    assert logs[0]["message_ts"] == "123.456"
    for hidden in (
        "secret-db-password",
        "secret-response-url",
        "secret-token",
        "private-notice-body",
        "private-button-value",
        USER_ID,
        WORKSPACE_ID,
        "DTEST",
    ):
        assert hidden not in caplog.text


def test_malformed_button_logs_rejection_without_echoing_arbitrary_fields(caplog):
    caplog.set_level(logging.INFO)
    service = Mock()
    create_checklist_action_handler(service)(
        RecordingAck(), RecordingAck(), {"token": "secret-token"}, logging.getLogger("test")
    )
    entry = json.loads(caplog.records[-1].getMessage())
    assert entry["event"] == "checklist_action_invalid_body"
    assert entry["trace_id"]
    assert "secret-token" not in caplog.text
    service.handle_action.assert_not_called()
