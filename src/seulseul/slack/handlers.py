"""Slack 명령어와 이벤트를 서비스에 넘기고 응답을 구성하는 핸들러."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App

from seulseul.notices.service import NoticeService
from seulseul.slack.client import (
    MessagePermalinkError,
    MessagePermalinkProvider,
    SlackCommandResponder,
    SlackWebApiClient,
    UserProfileError,
    UserProfileProvider,
)
from seulseul.slack.views import (
    build_command_help_text,
    build_enrollment_success_text,
    build_invalid_real_name_text,
    build_recent_notices_text,
    build_withdrawal_text,
)
from seulseul.users.service import InvalidStudentRealNameError, StudentService

SEULSEUL_COMMAND = "/seulseul"
START_ACTION = "시작"
WITHDRAW_ACTION = "해지"
NOTICE_ACTIONS = frozenset({"", "공지"})
RECENT_NOTICES_LIMIT = 5


def create_seulseul_command_handler(
    notice_service: NoticeService,
    student_service: StudentService,
    profile_provider: UserProfileProvider,
) -> Callable[..., None]:
    def handle_seulseul_command(
        ack: Callable[..., None],
        respond: Callable[..., None],
        command: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        ack()
        responder = SlackCommandResponder(respond)
        action = str(command.get("text") or "").strip()
        workspace_id = str(command.get("team_id") or "")
        user_id = str(command.get("user_id") or "")
        if not workspace_id or not user_id:
            responder.send("워크스페이스 또는 사용자 정보를 확인할 수 없습니다.")
            logger.warning("명령어 식별자 누락: action=%s", action)
            return

        if action == START_ACTION:
            _enroll_student(student_service, profile_provider, workspace_id, user_id, responder)
        elif action == WITHDRAW_ACTION:
            deleted = student_service.withdraw(workspace_id, user_id)
            responder.send(build_withdrawal_text(user_id, deleted))
        elif action in NOTICE_ACTIONS:
            notices = notice_service.recent_notices(
                RECENT_NOTICES_LIMIT,
                workspace_id=workspace_id,
            )
            responder.send(build_recent_notices_text(user_id, notices))
        else:
            responder.send(build_command_help_text(user_id))
        logger.info(
            "명령어 처리: command=%s action=%s channel=%s",
            command.get("command"),
            action,
            command.get("channel_id"),
        )

    return handle_seulseul_command


def _enroll_student(
    student_service: StudentService,
    profile_provider: UserProfileProvider,
    workspace_id: str,
    user_id: str,
    responder: SlackCommandResponder,
) -> None:
    try:
        real_name = profile_provider.get_real_name(user_id)
    except UserProfileError:
        responder.send(
            f"<@{user_id}> Slack 성명을 확인하지 못했습니다. "
            "앱의 `users:read` 권한과 프로필 설정을 확인해 주세요."
        )
        return
    try:
        student = student_service.enroll(workspace_id, user_id, real_name)
    except InvalidStudentRealNameError:
        responder.send(build_invalid_real_name_text(user_id))
        return
    responder.send(build_enrollment_success_text(user_id, student))


def create_message_event_handler(
    notice_service: NoticeService, permalink_provider: MessagePermalinkProvider
) -> Callable[..., None]:
    def handle_message_event(
        event: dict[str, Any],
        body: Mapping[str, Any],
        context: Mapping[str, Any],
        logger: logging.Logger,
    ) -> None:
        bot_user_id = str(context.get("bot_user_id") or "") or None
        bot_id = str(context.get("bot_id") or "") or None
        if not notice_service.accepts_message(event, bot_user_id, bot_id):
            logger.info(
                "메시지 이벤트 제외: channel=%s ts=%s",
                event.get("channel"),
                event.get("ts"),
            )
            return

        try:
            source_permalink = permalink_provider.get_message_permalink(
                str(event["channel"]), str(event["ts"])
            )
        except MessagePermalinkError as error:
            logger.warning(
                "공지 원문 링크 조회 실패: channel=%s ts=%s 원인=%s",
                event.get("channel"),
                event.get("ts"),
                error,
            )
            return

        workspace_id = str(body.get("team_id") or context.get("team_id") or "")
        if not workspace_id:
            logger.warning(
                "공지 workspace ID 누락: channel=%s ts=%s",
                event.get("channel"),
                event.get("ts"),
            )
            return

        notices = notice_service.record_channel_message(
            event,
            workspace_id=workspace_id,
            source_permalink=source_permalink,
            bot_user_id=bot_user_id,
            bot_id=bot_id,
        )
        logger.info(
            "메시지 이벤트 처리: channel=%s ts=%s notices=%d processed=%d failed=%d",
            event.get("channel"),
            event.get("ts"),
            len(notices),
            sum(notice.processing_status == "processed" for notice in notices),
            sum(notice.processing_status == "processing_failed" for notice in notices),
        )

    return handle_message_event


def register_handlers(
    app: App,
    notice_service: NoticeService,
    student_service: StudentService,
) -> None:
    permalink_provider = SlackWebApiClient(app.client)
    app.command(SEULSEUL_COMMAND)(
        create_seulseul_command_handler(notice_service, student_service, permalink_provider)
    )
    app.event("message")(create_message_event_handler(notice_service, permalink_provider))
