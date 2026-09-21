"""SeulSeul 봇 실행 진입점.

실행: .venv/bin/python -m seulseul.main
Slack Socket Mode로 연결하므로 공개 URL 없이 로컬에서 실행할 수 있다.
같은 앱 토큰으로 여러 프로세스를 동시에 실행하면 이벤트가 임의의 프로세스로 나뉘어 전달된다.
수집한 공지는 PostgreSQL에 저장한다.
"""

import logging
import sys
from collections.abc import Callable
from threading import Event

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from seulseul.ai.client import OpenAICompatibleChatClient
from seulseul.ai.service import NoticeAnalyzer
from seulseul.checklists.repository import SqlAlchemyChecklistRepository
from seulseul.checklists.service import DailyChecklistService
from seulseul.config import (
    AiSettings,
    ConfigError,
    SlackSettings,
    load_ai_settings,
    load_database_settings,
    load_notice_targets,
    load_slack_settings,
)
from seulseul.database import create_database_engine, create_session_factory
from seulseul.jobs.scheduler import ChecklistScheduler
from seulseul.jobs.tasks import refresh_checklists, retry_notices
from seulseul.notices.repository import NoticeRepository, SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeService
from seulseul.slack.client import SlackChecklistClient, SlackWebApiClient
from seulseul.slack.handlers import register_checklist_handlers, register_handlers
from seulseul.users.repository import SqlAlchemyStudentRepository
from seulseul.users.service import StudentService

logger = logging.getLogger(__name__)


def create_notice_service(
    ai_settings: AiSettings | None,
    notice_channel_ids: tuple[str, ...],
    repository: NoticeRepository,
    on_change: Callable[[], None] = lambda: None,
    permalink_resolver: Callable[[str, str], str] | None = None,
) -> NoticeService:
    if ai_settings is None:
        logger.info("AI_PROVIDER가 비어 있어 원문 링크만 수집합니다.")
        return NoticeService(
            allowed_channel_ids=notice_channel_ids, repository=repository, on_change=on_change
        )

    client = OpenAICompatibleChatClient(
        base_url=ai_settings.base_url,
        api_key=ai_settings.api_key,
        model=ai_settings.model,
        timeout_seconds=ai_settings.timeout_seconds,
        provider=ai_settings.provider,
        disable_thinking=True,
    )
    # API 키는 로그에 남기지 않는다.
    logger.info(
        "AI 요약 사용: provider=%s model=%s base_url=%s",
        ai_settings.provider,
        ai_settings.model,
        ai_settings.base_url,
    )
    return NoticeService(
        allowed_channel_ids=notice_channel_ids,
        analyzer=NoticeAnalyzer(client),
        repository=repository,
        on_change=on_change,
        permalink_resolver=permalink_resolver,
    )


def create_app(
    settings: SlackSettings,
    notice_service: NoticeService,
    student_service: StudentService,
) -> App:
    # App을 만들 때 Bolt가 auth.test로 Bot 토큰을 검증하므로 네트워크 연결이 필요하다.
    app = App(token=settings.bot_token)
    register_handlers(app, notice_service, student_service)
    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        slack_settings = load_slack_settings()
        ai_settings = load_ai_settings()
        database_settings = load_database_settings()
        notice_targets = load_notice_targets(slack_settings.notice_channels)
    except ConfigError as error:
        logger.error("설정 오류: %s", error)
        sys.exit(1)

    engine = create_database_engine(database_settings)
    session_factory = create_session_factory(engine)
    notice_repository = SqlAlchemyNoticeRepository(session_factory)
    student_repository = SqlAlchemyStudentRepository(session_factory)
    notice_service = create_notice_service(
        ai_settings,
        slack_settings.notice_channels,
        notice_repository,
        (wakeup := Event()).set,
        SlackWebApiClient.from_token(slack_settings.bot_token).get_message_permalink,
    )
    messenger = SlackChecklistClient.from_token(slack_settings.bot_token)
    checklist_service = DailyChecklistService(
        SqlAlchemyChecklistRepository(session_factory),
        messenger,
        (workspace_id := messenger.workspace_id()),
        notice_targets,
        notify=wakeup.set,
    )
    student_service = StudentService(
        student_repository,
        on_change=wakeup.set,
        reset_messages=checklist_service.reset_messages,
        on_withdraw=messenger.send_withdrawal,
        on_invalid_name=messenger.send_enrollment_guidance,
        profile_update=checklist_service.profile_update,
    )
    app = create_app(slack_settings, notice_service, student_service)
    register_checklist_handlers(app, checklist_service)
    scheduler = ChecklistScheduler(
        lambda: refresh_checklists(checklist_service, scheduler.stopped.is_set),
        wakeup,
    )
    retry_scheduler = ChecklistScheduler(
        lambda: retry_notices(notice_service, workspace_id, retry_scheduler.stopped.is_set),
        Event(),
    )
    logger.info(
        "Socket Mode로 Slack에 연결합니다. 공지 채널 설정 %d개",
        len(slack_settings.notice_channels),
    )
    scheduler.start()
    retry_scheduler.start()
    try:
        SocketModeHandler(app, slack_settings.app_token).start()
    finally:
        scheduler.stop()
        retry_scheduler.stop()
        engine.dispose()


if __name__ == "__main__":
    main()
