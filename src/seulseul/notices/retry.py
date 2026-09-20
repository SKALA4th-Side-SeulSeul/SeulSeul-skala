"""운영자용 실패 공지 조회·선택 재처리 명령. Slack 연결은 새로 만들지 않는다."""

import argparse
import logging
import re
import shlex
from contextlib import ExitStack

from sqlalchemy.exc import SQLAlchemyError

from seulseul.ai.client import OpenAICompatibleChatClient
from seulseul.ai.service import NoticeAnalyzer
from seulseul.config import (
    ConfigError,
    load_ai_settings,
    load_database_settings,
    load_slack_settings,
)
from seulseul.database import create_database_engine, create_session_factory
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeRetryError, NoticeService


def _positive_limit(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("1~100 사이의 정수를 입력하세요.") from error
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("1~100 사이의 정수를 입력하세요.")
    return value


def _safe_error(message: str | None) -> str:
    # 제공자의 원시 응답에는 입력 원문이 포함될 수 있어 표시하지 않는다.
    message = (message or "실패 원인이 기록되어 있지 않습니다.").split(", 응답:", 1)[0]
    message = re.sub(r"https?://\S+|(?:nvapi-|xox[baprs]-)[A-Za-z0-9_-]+", "[숨김]", message)
    return " ".join(message.split())[:500]


def run_command(service: NoticeService, args: argparse.Namespace) -> int:
    if args.action == "list":
        notices = service.failed_notices(args.limit, workspace_id=args.workspace_id)
        if not notices:
            print("현재 설정된 채널에 재처리할 실패 공지가 없습니다.")
            return 0
        for notice in notices:
            print(f"워크스페이스: {notice.workspace_id}")
            print(f"원문: {notice.source_permalink}")
            print(f"실패 원인: {_safe_error(notice.last_error)}")
            print(
                "재처리 명령: "
                + shlex.join(
                    [
                        ".venv/bin/python",
                        "-m",
                        "seulseul.notices.retry",
                        "retry",
                        "--workspace-id",
                        notice.workspace_id,
                        "--url",
                        notice.original_url,
                        "--channel-id",
                        notice.channel_id,
                        "--message-ts",
                        notice.message_ts,
                    ]
                )
            )
        if len(notices) == args.limit:
            print("조회 한도에 도달했습니다. --limit 또는 --workspace-id로 범위를 조정하세요.")
        return 0

    notice = service.retry_failed_notice(
        args.workspace_id,
        args.url,
        getattr(args, "channel_id", None),
        getattr(args, "message_ts", None),
    )
    if notice.processing_status == "processed":
        print(
            "재처리 성공: 기존 공지의 분석 결과를 갱신했습니다. "
            "실행 중인 봇이 기존 체크리스트 DM에 반영합니다."
        )
        return 0
    print(f"재처리 실패: {_safe_error(notice.last_error)}")
    print("최근 실패 원인을 저장했습니다. AI 서버나 설정을 확인한 뒤 다시 실행해 주세요.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    listing = commands.add_parser("list", help="실패 공지와 재처리 명령 조회 (AI 호출 없음)")
    listing.add_argument("--workspace-id")
    listing.add_argument("--limit", type=_positive_limit, default=20)
    retrying = commands.add_parser("retry", help="선택한 실패 공지 하나 재처리")
    retrying.add_argument("--workspace-id", required=True)
    retrying.add_argument("--url", required=True, help="공지의 제출 링크 (Slack 원문 링크 아님)")
    retrying.add_argument("--channel-id", help="재처리할 원본 채널 ID")
    retrying.add_argument("--message-ts", help="재처리할 원본 메시지 ts")
    args = parser.parse_args(argv)

    # CLI에서는 아래의 정제한 오류만 표시하고 원시 AI 오류 응답을 로그로 노출하지 않는다.
    logging.basicConfig(level=logging.ERROR)
    try:
        with ExitStack() as resources:
            channels = load_slack_settings().notice_channels
            engine = create_database_engine(load_database_settings())
            resources.callback(engine.dispose)
            repository = SqlAlchemyNoticeRepository(create_session_factory(engine))
            analyzer = None
            if args.action == "retry":
                settings = load_ai_settings()
                if settings is None:
                    raise NoticeRetryError("AI_PROVIDER를 설정한 뒤 다시 실행해 주세요.")
                client = OpenAICompatibleChatClient(
                    base_url=settings.base_url,
                    api_key=settings.api_key,
                    model=settings.model,
                    timeout_seconds=settings.timeout_seconds,
                    disable_thinking=settings.provider == "nvidia",
                )
                resources.callback(client.close)
                analyzer = NoticeAnalyzer(client)
            service = NoticeService(channels, analyzer=analyzer, repository=repository)
            return run_command(service, args)
    except (ConfigError, NoticeRetryError) as error:
        print(_safe_error(str(error)))
        return 1
    except SQLAlchemyError:
        print("DB 작업에 실패했습니다. PostgreSQL 실행 상태와 DATABASE_URL을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
