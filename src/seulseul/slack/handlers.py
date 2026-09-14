"""Slack 명령어와 이벤트를 서비스에 넘기고 응답을 구성하는 핸들러.

핸들러는 입력 전달과 응답 구성만 담당한다. 공지 판별과 요약 규칙은 notices·ai 서비스에 둔다.
현재는 공지 수집·조회 연결 테스트용 임시 핸들러다.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

from slack_bolt import App

from seulseul.notices.service import NoticeService
from seulseul.slack.views import build_recent_notices_text

# 개발자마다 앱과 명령어 이름이 달라 연결 테스트 단계에서는 모든 슬래시 명령어를 받는다.
# 실제 기능 명령어를 추가할 때 구체적인 이름으로 바꾼다.
ANY_SLASH_COMMAND = re.compile(r"^/.+")
RECENT_NOTICES_LIMIT = 5


def create_recent_notices_command_handler(
    notice_service: NoticeService,
) -> Callable[..., None]:
    # Bolt는 매개변수 이름(ack, command, logger)으로 값을 넣어 주므로 이름을 바꾸지 않는다.
    def handle_recent_notices_command(
        ack: Callable[..., None], command: dict[str, Any], logger: logging.Logger
    ) -> None:
        notices = notice_service.recent_notices(RECENT_NOTICES_LIMIT)
        # 명령어 요청에 바로 응답(ack)하지 않으면 Slack이 사용자에게 오류를 보여 준다.
        # 요약은 공지를 받을 때 미리 만들어 두므로 여기서는 AI를 호출하지 않는다.
        ack(build_recent_notices_text(command["user_id"], notices))
        logger.info(
            "명령어 수신: command=%s channel=%s notices=%d",
            command.get("command"),
            command.get("channel_id"),
            len(notices),
        )

    return handle_recent_notices_command


def create_message_event_handler(notice_service: NoticeService) -> Callable[..., None]:
    def handle_message_event(event: dict[str, Any], logger: logging.Logger) -> None:
        notice = notice_service.record_channel_message(event)
        # 공지 본문에는 개인정보가 있을 수 있어 채널 ID, 메시지 시각, 처리 결과만 기록한다.
        logger.info(
            "메시지 이벤트 수신: channel=%s ts=%s stored=%s summarized=%s",
            event.get("channel"),
            event.get("ts"),
            notice is not None,
            notice is not None and notice.summary is not None,
        )

    return handle_message_event


def register_handlers(app: App, notice_service: NoticeService) -> None:
    app.command(ANY_SLASH_COMMAND)(create_recent_notices_command_handler(notice_service))
    app.event("message")(create_message_event_handler(notice_service))
