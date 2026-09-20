"""Slack 명령어와 이벤트를 서비스에 넘기고 응답을 구성하는 핸들러."""

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App

from seulseul.checklists.model import ChecklistActionError, ChecklistDeliveryError
from seulseul.checklists.service import DailyChecklistService
from seulseul.notices.service import NoticeService
from seulseul.slack.client import (
    MessagePermalinkError,
    MessagePermalinkProvider,
    SlackCommandResponder,
    SlackWebApiClient,
    UserProfileError,
    UserProfileProvider,
)
from seulseul.users.service import InvalidStudentRealNameError, StudentService

SEULSEUL_COMMAND = "/seulseul"
START_ACTION = "시작"
WITHDRAW_ACTION = "해지"


def create_seulseul_command_handler(
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
        action = str(command.get("text") or "").strip()
        workspace_id = str(command.get("team_id") or "")
        user_id = str(command.get("user_id") or "")
        if not workspace_id or not user_id:
            logger.warning("명령어 식별자 누락: action=%s", action)
            return

        try:
            if action == START_ACTION:
                _enroll_student(student_service, profile_provider, workspace_id, user_id, logger)
            elif action == WITHDRAW_ACTION:
                student_service.withdraw(workspace_id, user_id)
            else:
                logger.info("지원하지 않는 슬래시 명령 입력")
        except ChecklistDeliveryError as error:
            logger.warning("명령 DM 처리 실패: code=%s", error.code)
            return
        except Exception as error:
            logger.error("슬래시 명령 처리 실패: type=%s", type(error).__name__)
            return
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
    logger: logging.Logger,
) -> None:
    try:
        student_service.enroll_from_profile(workspace_id, user_id, profile_provider.get_real_name)
    except UserProfileError:
        logger.warning("가입 실패: Slack 성명 조회 실패; users:read 권한·프로필 확인 필요")
        return
    except InvalidStudentRealNameError:
        logger.warning("가입 실패: Slack 성명 형식 불일치")
        return


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
        parsed = notice_service.parse_event(event, bot_user_id, bot_id)
        if parsed is None:
            logger.info(
                "메시지 이벤트 제외: channel=%s ts=%s",
                event.get("channel"),
                event.get("ts"),
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

        source_permalink = ""
        collection_error = None
        collection_retryable = False
        if notice_service.needs_permalink(parsed):
            source_permalink = notice_service.stored_permalink(workspace_id, parsed) or ""
            if not source_permalink:
                try:
                    source_permalink = permalink_provider.get_message_permalink(
                        parsed.channel_id, parsed.message_ts
                    )
                except MessagePermalinkError as error:
                    logger.warning(
                        "공지 원문 링크 조회 실패: channel=%s ts=%s 원인=%s",
                        parsed.channel_id,
                        parsed.message_ts,
                        error,
                    )
                    collection_error = "Slack 원문 링크 조회 실패: 운영자 CLI로 재처리해 주세요."
                    collection_retryable = error.auto_retryable
                    if collection_retryable:
                        collection_error = (
                            "Slack 원문 링크 일시 조회 실패: 자동 재처리 예약 대상입니다."
                        )

        notices = notice_service.record_channel_message(
            event,
            workspace_id=workspace_id,
            source_permalink=source_permalink,
            bot_user_id=bot_user_id,
            bot_id=bot_id,
            collection_error=collection_error,
            collection_retryable=collection_retryable,
        )
        logger.info(
            "메시지 이벤트 처리: channel=%s ts=%s notices=%d processed=%d failed=%d",
            event.get("channel"),
            parsed.message_ts,
            len(notices),
            sum(notice.processing_status == "processed" for notice in notices),
            sum(notice.processing_status == "processing_failed" for notice in notices),
        )

    return handle_message_event


def create_user_change_handler(
    student_service: StudentService, profile_provider: UserProfileProvider
) -> Callable[..., None]:
    def handle_user_change(event, body, context, logger):
        if event.get("type") != "user_change":
            return
        user = event.get("user")
        if not isinstance(user, Mapping):
            return
        user_id = user.get("id")
        workspace_id = body.get("team_id") or context.get("team_id")
        if not isinstance(user_id, str) or not re.fullmatch(r"[UW][A-Z0-9]+", user_id):
            return
        if not isinstance(workspace_id, str) or not workspace_id:
            return
        if context.get("team_id") and context["team_id"] != workspace_id:
            return
        try:
            student_service.sync_profile(workspace_id, user_id, profile_provider.get_real_name)
        except Exception as error:
            # 성명/예외 본문에는 개인정보가 있을 수 있으므로 종류만 기록한다.
            logger.warning("학생 소속 동기화 실패: type=%s", type(error).__name__)

    return handle_user_change


def register_handlers(
    app: App,
    notice_service: NoticeService,
    student_service: StudentService,
) -> None:
    permalink_provider = SlackWebApiClient(app.client)
    app.command(SEULSEUL_COMMAND)(
        create_seulseul_command_handler(student_service, permalink_provider)
    )
    app.event("message")(create_message_event_handler(notice_service, permalink_provider))
    app.event("user_change")(create_user_change_handler(student_service, permalink_provider))


def create_checklist_action_handler(service: DailyChecklistService) -> Callable[..., None]:
    def handle_checklist_action(
        ack: Callable[..., None],
        respond: Callable[..., None],
        body: dict[str, Any],
        logger: logging.Logger,
    ) -> None:
        ack()
        responder = SlackCommandResponder(respond)
        try:
            action = body["actions"][0]
            operation = str(action["action_id"]).removeprefix("checklist_")
            updated = service.handle_action(
                str(body["team"]["id"]),
                str(body["user"]["id"]),
                str(body["container"]["channel_id"]),
                str(body["container"]["message_ts"]),
                operation,
                str(action["value"]),
            )
            if not updated:
                responder.send("변경은 저장했습니다. DM 갱신을 기다리거나 새로고침을 눌러 주세요.")
        except (KeyError, IndexError, TypeError):
            responder.send("버튼 정보를 확인할 수 없습니다. 최신 체크리스트를 이용해 주세요.")
        except ChecklistActionError as error:
            responder.send(str(error))
        except Exception as error:
            logger.error("체크리스트 동작 오류: type=%s", type(error).__name__)
            responder.send("처리 결과를 확인하지 못했습니다. 잠시 후 새로고침해 주세요.")

    return handle_checklist_action


def register_checklist_handlers(app: App, service: DailyChecklistService) -> None:
    app.action(re.compile(r"^checklist_(complete|undo|pending|completed|previous|next|refresh)$"))(
        create_checklist_action_handler(service)
    )
