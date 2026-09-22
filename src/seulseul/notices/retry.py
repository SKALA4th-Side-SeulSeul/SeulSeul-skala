"""운영자용 실패 공지 조회·선택 재처리. 봇은 시작하지 않고 필요 시 원문 링크만 조회한다."""

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
from seulseul.slack.client import SlackWebApiClient


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
    if args.action == "pending":
        sources = service.pending_sources(args.limit, workspace_id=args.workspace_id)
        if not sources:
            print("현재 설정된 채널에 미적용 원본이 없습니다.")
            return 0
        for workspace, channel, message_ts in sources:
            print(f"워크스페이스: {workspace} · 채널: {channel} · 메시지 ts: {message_ts}")
        print(
            "미적용 원본에는 정상 처리 중인 이벤트도 포함됩니다. 잠시 후 다시 조회하세요.\n"
            "이벤트 원문이 보관되어 있지 않아 이 명령으로 자동 복구할 수 없습니다. "
            "계속 남아 있는 원본은 Slack에서 내용을 확인하고 실제로 다시 수정해 주세요. "
            "삭제된 원본은 수정할 수 없으므로 운영자가 별도로 상태를 확인해야 합니다. "
            "기존 본문 강제 재처리나 DB 상태 초기화는 수행하지 않습니다."
        )
        if len(sources) == args.limit:
            print("조회 한도에 도달했습니다. --limit 또는 --workspace-id로 범위를 조정하세요.")
        return 0
    if args.action == "list":
        notices = service.failed_notices(args.limit, workspace_id=args.workspace_id)
        if not notices:
            print("현재 설정된 채널에 재처리할 실패 공지가 없습니다.")
            return 0
        for index, notice in enumerate(notices, 1):
            print(f"{index}. 워크스페이스: {notice.workspace_id}")
            print(f"원문: {notice.source_permalink or '조회 실패 — 재처리 시 다시 조회합니다.'}")
            print(f"실패 원인: {_safe_error(notice.last_error)}")
            print(
                f"자동 재처리 예약: {notice.next_retry_at.isoformat()} · "
                f"실행 {notice.retry_count}/3회"
                if notice.next_retry_at
                else "자동 재처리 예약 없음 (수동 확인 대상)"
            )
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

    index = getattr(args, "index", None)
    if index is not None:
        notices = service.failed_notices(
            getattr(args, "limit", 20), workspace_id=getattr(args, "workspace_id", None)
        )
        if not 1 <= index <= len(notices):
            print(f"선택한 번호가 없습니다. 1~{len(notices)} 사이에서 선택하세요.")
            return 1
        selected = notices[index - 1]
        workspace_id = selected.workspace_id
        url = selected.original_url
        channel_id = selected.channel_id
        message_ts = selected.message_ts
        print(f"번호 {index} 공지를 재처리합니다.")
    else:
        workspace_id = args.workspace_id
        url = args.url
        channel_id = getattr(args, "channel_id", None)
        message_ts = getattr(args, "message_ts", None)
    notice = service.retry_failed_notice(workspace_id, url, channel_id, message_ts)
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
    pending = commands.add_parser(
        "pending", help="미적용 원본 식별자 조회 (상태 변경·AI 호출 없음)"
    )
    pending.add_argument("--workspace-id")
    pending.add_argument("--limit", type=_positive_limit, default=20)
    retrying = commands.add_parser("retry", help="선택한 실패 공지 하나 재처리")
    retrying.add_argument("--index", type=_positive_limit, help="list와 같은 목록의 번호")
    retrying.add_argument("--limit", type=_positive_limit, default=20)
    retrying.add_argument("--workspace-id", help="직접 지정하거나 --index와 함께 선택 범위를 좁힘")
    retrying.add_argument("--url", help="공지의 제출 링크 (Slack 원문 링크 아님)")
    retrying.add_argument("--channel-id", help="재처리할 원본 채널 ID")
    retrying.add_argument("--message-ts", help="재처리할 원본 메시지 ts")
    args = parser.parse_args(argv)
    if args.action == "retry":
        explicit = (
            args.url is not None or args.channel_id is not None or args.message_ts is not None
        )
        if args.index is None and (args.workspace_id is None or args.url is None):
            parser.error("retry에는 --index 또는 --workspace-id와 --url이 필요합니다.")
        if args.index is not None and explicit:
            parser.error(
                "--index를 사용할 때 --url·--channel-id·--message-ts를 함께 지정하지 마세요."
            )

    # CLI에서는 아래의 정제한 오류만 표시하고 원시 AI 오류 응답을 로그로 노출하지 않는다.
    logging.basicConfig(level=logging.ERROR)
    try:
        with ExitStack() as resources:
            slack_settings = load_slack_settings()
            channels = (*slack_settings.notice_channels, *slack_settings.manual_notice_channels)
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
                    provider=settings.provider,
                    disable_thinking=True,
                )
                resources.callback(client.close)
                analyzer = NoticeAnalyzer(client)
            service = NoticeService(
                channels,
                analyzer=analyzer,
                repository=repository,
                permalink_resolver=SlackWebApiClient.from_token(
                    slack_settings.bot_token
                ).get_message_permalink
                if args.action == "retry"
                else None,
            )
            return run_command(service, args)
    except (ConfigError, NoticeRetryError) as error:
        print(_safe_error(str(error)))
        return 1
    except SQLAlchemyError:
        print("DB 작업에 실패했습니다. PostgreSQL 실행 상태와 DATABASE_URL을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
