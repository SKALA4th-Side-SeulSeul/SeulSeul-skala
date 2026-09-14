"""Slack 명령어와 이벤트를 서비스에 넘기고 응답을 구성하는 핸들러."""

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from slack_bolt import App

from seulseul.notices.service import NoticeService
from seulseul.slack.client import (
    MessagePermalinkError,
    MessagePermalinkProvider,
    SlackWebApiClient,
)
from seulseul.slack.views import build_recent_notices_text

ANY_SLASH_COMMAND = re.compile(r"^/.+")
RECENT_NOTICES_LIMIT = 5


def create_recent_notices_command_handler(
    notice_service: NoticeService,
) -> Callable[..., None]:
    def handle_recent_notices_command(
        ack: Callable[..., None], command: dict[str, Any], logger: logging.Logger
    ) -> None:
        notices = notice_service.recent_notices(RECENT_NOTICES_LIMIT)
        ack(build_recent_notices_text(command["user_id"], notices))
        logger.info(
            "명령어 수신: command=%s channel=%s notices=%d",
            command.get("command"),
            command.get("channel_id"),
            len(notices),
        )

    return handle_recent_notices_command


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


def register_handlers(app: App, notice_service: NoticeService) -> None:
    app.command(ANY_SLASH_COMMAND)(create_recent_notices_command_handler(notice_service))
    permalink_provider = SlackWebApiClient(app.client)
    app.event("message")(create_message_event_handler(notice_service, permalink_provider))
