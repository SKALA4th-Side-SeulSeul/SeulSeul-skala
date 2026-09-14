"""SeulSeul 봇 실행 진입점.

실행: .venv/bin/python -m seulseul.main
Slack Socket Mode로 연결하므로 공개 URL 없이 로컬에서 실행할 수 있다.
같은 앱 토큰으로 여러 프로세스를 동시에 실행하면 이벤트가 임의의 프로세스로 나뉘어 전달된다.
수집한 공지는 메모리에만 보관하므로 봇을 다시 실행하면 비워진다.
"""

import logging
import sys

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from seulseul.ai.client import OpenAICompatibleChatClient
from seulseul.ai.service import NoticeSummarizer
from seulseul.config import (
    AiSettings,
    ConfigError,
    SlackSettings,
    load_ai_settings,
    load_slack_settings,
)
from seulseul.notices.service import NoticeService
from seulseul.slack.handlers import register_handlers

logger = logging.getLogger(__name__)


def create_notice_service(ai_settings: AiSettings | None) -> NoticeService:
    if ai_settings is None:
        logger.info("AI_PROVIDER가 비어 있어 AI 요약 없이 공지 원문만 수집합니다.")
        return NoticeService()

    client = OpenAICompatibleChatClient(
        base_url=ai_settings.base_url,
        api_key=ai_settings.api_key,
        model=ai_settings.model,
        timeout_seconds=ai_settings.timeout_seconds,
    )
    # API 키는 로그에 남기지 않는다.
    logger.info(
        "AI 요약 사용: provider=%s model=%s base_url=%s",
        ai_settings.provider,
        ai_settings.model,
        ai_settings.base_url,
    )
    return NoticeService(summarizer=NoticeSummarizer(client))


def create_app(settings: SlackSettings, notice_service: NoticeService) -> App:
    # App을 만들 때 Bolt가 auth.test로 Bot 토큰을 검증하므로 네트워크 연결이 필요하다.
    app = App(token=settings.bot_token)
    register_handlers(app, notice_service)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        slack_settings = load_slack_settings()
        ai_settings = load_ai_settings()
    except ConfigError as error:
        logger.error("설정 오류: %s", error)
        sys.exit(1)

    app = create_app(slack_settings, create_notice_service(ai_settings))
    logger.info(
        "Socket Mode로 Slack에 연결합니다. 공지 채널 설정 %d개",
        len(slack_settings.notice_channels),
    )
    SocketModeHandler(app, slack_settings.app_token).start()


if __name__ == "__main__":
    main()
