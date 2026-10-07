"""운영자가 Slack 원문을 수동 등록·수정·삭제하는 CLI.

봇이 없거나 설정에 없는 채널의 원문은 --target-channel-id로 학생 배정 기준 채널(설정된
공지 채널)을 지정한다. 수정·삭제도 등록 때와 같은 배정 기준 채널을 지정해야 같은 원본으로 찾는다.
워크스페이스는 생략하면 봇 토큰 기준으로 자동 확인한다(D-042).
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from seulseul.ai.client import OpenAICompatibleChatClient
from seulseul.ai.model import NoticeAnalysis
from seulseul.ai.service import NoticeAnalyzer
from seulseul.checklists.model import ChecklistDeliveryError
from seulseul.config import (
    ConfigError,
    load_ai_settings,
    load_database_settings,
    load_slack_settings,
)
from seulseul.database import create_database_engine, create_session_factory
from seulseul.logging_setup import configure_logging
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeRetryError, NoticeService
from seulseul.slack.client import SlackChecklistClient

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")
SLACK_MESSAGE_PATH = re.compile(r"^/archives/(?P<channel>[CG][A-Z0-9]+)/p(?P<stamp>[0-9]{16})$")
MAX_MANUAL_TEXT_CHARS = 12_000


def parse_slack_permalink(source_url: str) -> tuple[str, str]:
    """Slack 메시지 permalink에서 채널 ID와 Slack timestamp를 추출한다."""
    try:
        parsed = urlsplit(source_url)
    except ValueError as error:
        raise ValueError("Slack 원문 링크 형식이 올바르지 않습니다.") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".slack.com")
    ):
        raise ValueError("Slack 원문 링크 형식이 올바르지 않습니다.")
    match = SLACK_MESSAGE_PATH.fullmatch(parsed.path)
    if match is None:
        raise ValueError("Slack 원문 링크 형식이 올바르지 않습니다.")
    stamp = match["stamp"]
    return match["channel"], f"{stamp[:10]}.{stamp[10:]}"


def build_manual_event(
    channel_id: str,
    message_ts: str,
    text: str = "",
    *,
    kind: str,
) -> dict[str, object]:
    """수동 입력을 기존 Slack message 이벤트 형태로 변환한다."""
    if kind == "created":
        return {
            "type": "message",
            "channel_type": "channel",
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
            "user": "U_MANUAL_OPERATOR",
        }
    if kind == "changed":
        revision = f"{max(time.time(), float(message_ts) + 0.000001):.6f}"
        return {
            "type": "message",
            "subtype": "message_changed",
            "channel": channel_id,
            "event_ts": revision,
            "message": {
                "type": "message",
                "channel": channel_id,
                "ts": message_ts,
                "text": text,
                "user": "U_MANUAL_OPERATOR",
                "edited": {"ts": revision},
            },
        }
    if kind == "deleted":
        revision = f"{max(time.time(), float(message_ts) + 0.000001):.6f}"
        return {
            "type": "message",
            "subtype": "message_deleted",
            "channel": channel_id,
            "event_ts": revision,
            "deleted_ts": message_ts,
            "previous_message": {"ts": message_ts},
        }
    raise ValueError("수동 공지 이벤트 종류가 올바르지 않습니다.")


def parse_manual_analysis(
    *,
    title: str,
    summary: str,
    deadline: str,
    deadline_source_text: str,
) -> NoticeAnalysis:
    """운영자 수동 분석값을 저장 가능한 구조로 변환한다."""
    title = title.strip()
    summary = summary.strip()
    deadline_source_text = deadline_source_text.strip()
    if not title or not summary or not deadline_source_text:
        raise ValueError("수동 제목·요약·마감일 근거는 비어 있을 수 없습니다.")
    if len(title) > 255 or "\x00" in title + summary + deadline_source_text:
        raise ValueError("수동 분석 결과가 저장 규격을 벗어났습니다.")
    normalized_deadline = deadline.strip().replace("Z", "+00:00")
    try:
        parsed_deadline = datetime.fromisoformat(normalized_deadline)
    except ValueError as error:
        raise ValueError("마감일은 YYYY-MM-DD HH:MM 또는 ISO 8601 형식이어야 합니다.") from error
    if parsed_deadline.tzinfo is None:
        parsed_deadline = parsed_deadline.replace(tzinfo=SEOUL)
    else:
        parsed_deadline = parsed_deadline.astimezone(SEOUL)
    return NoticeAnalysis(title, summary, parsed_deadline, deadline_source_text)


def _read_text(path: str) -> str:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    text = text.strip()
    if not text:
        raise ValueError("공지 원문이 비어 있습니다.")
    if len(text) > MAX_MANUAL_TEXT_CHARS:
        raise ValueError(f"공지 원문은 {MAX_MANUAL_TEXT_CHARS}자 이하이어야 합니다.")
    return text


def _manual_analysis_from_args(args: argparse.Namespace) -> NoticeAnalysis | None:
    values = [args.title, args.summary, args.deadline, args.deadline_source_text]
    if not any(value is not None for value in values):
        return None
    if any(value is None for value in values[:3]):
        raise ValueError("수동 입력은 --title, --summary, --deadline을 함께 지정해야 합니다.")
    source_text = args.deadline_source_text or args.deadline
    return parse_manual_analysis(
        title=args.title,
        summary=args.summary,
        deadline=args.deadline,
        deadline_source_text=source_text,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("add", "edit"):
        command = commands.add_parser(
            action,
            help="새 공지 등록" if action == "add" else "기존 공지 수정",
        )
        _add_source_arguments(command)
        command.add_argument(
            "--text-file", required=True, help="공지 원문 파일 경로; -는 표준 입력"
        )
        command.add_argument("--title", help="AI 실패 시 사용할 수동 제목")
        command.add_argument("--summary", help="AI 실패 시 사용할 수동 요약")
        command.add_argument("--deadline", help="AI 실패 시 사용할 마감일(서울 시간)")
        command.add_argument("--deadline-source-text", help="마감일 근거 원문")
    deleting = commands.add_parser("delete", help="공지 삭제 처리")
    _add_source_arguments(deleting)
    return parser


def _add_source_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--workspace-id", help="생략하면 봇 토큰 기준으로 자동 확인")
    command.add_argument("--source-url", required=True, help="Slack 원문 permalink")
    command.add_argument(
        "--target-channel-id",
        help="봇이 없거나 설정에 없는 채널의 원문을 배정할 설정된 공지 채널 ID",
    )


def target_channel(source_channel: str, requested: str | None, allowed: tuple[str, ...]) -> str:
    """원문 채널이 설정에 있으면 그대로, 없으면 지정한 배정 기준 채널을 쓴다."""
    if source_channel in allowed:
        if requested not in {None, source_channel}:
            raise NoticeRetryError(
                "원문 채널이 이미 공지 채널 설정에 있어 --target-channel-id를 쓰지 않습니다."
            )
        return source_channel
    if requested is None:
        raise NoticeRetryError(
            "봇이 없거나 설정에 없는 채널의 원문입니다. --target-channel-id로 학생 배정 기준 "
            f"채널을 지정하세요. 지정 가능: {', '.join(allowed) or '없음'}"
        )
    if requested not in allowed:
        raise NoticeRetryError(
            f"--target-channel-id는 설정된 공지 채널이어야 합니다. 지정 가능: {', '.join(allowed)}"
        )
    return requested


def detect_workspace(bot_token: str) -> str:
    try:
        return SlackChecklistClient.from_token(bot_token).workspace_id()
    except ChecklistDeliveryError as error:
        raise NoticeRetryError(
            f"워크스페이스를 자동으로 확인하지 못했습니다({error.code}). "
            "--workspace-id를 지정하세요."
        ) from error


def _create_service(
    args: argparse.Namespace,
    settings,
    repository: SqlAlchemyNoticeRepository,
    resources: ExitStack,
    manual_analysis: NoticeAnalysis | None,
) -> NoticeService:
    analyzer = None
    if args.action != "delete" and manual_analysis is None:
        ai_settings = load_ai_settings()
        if ai_settings is None:
            raise NoticeRetryError(
                "AI_PROVIDER를 설정하거나 --title --summary --deadline 수동 입력을 사용하세요."
            )
        client = OpenAICompatibleChatClient(
            base_url=ai_settings.base_url,
            api_key=ai_settings.api_key,
            model=ai_settings.model,
            timeout_seconds=ai_settings.timeout_seconds,
            provider=ai_settings.provider,
            disable_thinking=True,
        )
        resources.callback(client.close)
        analyzer = NoticeAnalyzer(client)
    allowed_channels = (*settings.notice_channels, *settings.manual_notice_channels)
    return NoticeService(allowed_channels, analyzer=analyzer, repository=repository)


def run_command(args: argparse.Namespace) -> int:
    source_channel, message_ts = parse_slack_permalink(args.source_url)
    manual_analysis = _manual_analysis_from_args(args) if args.action != "delete" else None
    text = _read_text(args.text_file) if args.action != "delete" else ""
    with ExitStack() as resources:
        settings = load_slack_settings()
        allowed_channels = (*settings.notice_channels, *settings.manual_notice_channels)
        channel_id = target_channel(source_channel, args.target_channel_id, allowed_channels)
        workspace_id = args.workspace_id or detect_workspace(settings.bot_token)
        engine = create_database_engine(load_database_settings())
        resources.callback(engine.dispose)
        repository = SqlAlchemyNoticeRepository(create_session_factory(engine))
        service = _create_service(args, settings, repository, resources, manual_analysis)
        if args.action == "delete":
            event = build_manual_event(channel_id, message_ts, kind="deleted")
        elif args.action == "add":
            event = build_manual_event(channel_id, message_ts, text, kind="created")
        else:
            event = build_manual_event(channel_id, message_ts, text, kind="changed")
        changed = service.record_channel_message(
            event,
            workspace_id=workspace_id,
            source_permalink=args.source_url,
            manual_analysis=manual_analysis,
        )
    if not changed:
        raise NoticeRetryError("공지 원본이 이미 처리되었거나 변경 결과가 없습니다.")
    if args.action != "delete" and any(
        notice.processing_status == "processing_failed" for notice in changed
    ):
        print(
            "공지 저장은 되었지만 AI 분석에 실패했습니다. 수동 분석값으로 edit을 다시 실행하세요."
        )
        return 1
    print("수동 공지 처리가 완료되었습니다. 실행 중인 봇이 학생 체크리스트 DM을 갱신합니다.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    configure_logging()
    try:
        result = run_command(args)
    except (ConfigError, NoticeRetryError, ValueError, OSError) as error:
        print(f"✖ 수동 공지 {args.action} 실패: {error}")
        logger.error(
            "수동 공지 처리 실패: action=%s stage=CLI error=%s reason=%s",
            args.action,
            type(error).__name__,
            error,
        )
        return 1
    except SQLAlchemyError as error:
        # DB 오류 문자열에는 접속 정보가 섞일 수 있어 오류 종류만 남긴다.
        print("DB 작업에 실패했습니다. PostgreSQL 실행 상태와 DATABASE_URL을 확인해 주세요.")
        logger.error(
            "수동 공지 처리 실패: action=%s stage=DB error=%s reason=DB 작업 실패",
            args.action,
            type(error).__name__,
        )
        return 1
    if result == 0:
        logger.info("수동 공지 처리 완료: action=%s", args.action)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
