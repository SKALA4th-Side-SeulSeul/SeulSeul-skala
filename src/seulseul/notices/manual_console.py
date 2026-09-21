"""운영자 공지 등록·수정·삭제를 한 번에 수행하는 대화형 도우미."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Collection
from contextlib import ExitStack
from dataclasses import replace

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
from seulseul.notices.manual import (
    MAX_MANUAL_TEXT_CHARS,
    build_manual_event,
    parse_manual_analysis,
    parse_slack_permalink,
)
from seulseul.notices.manual_edit import EditCancelled, _display, _field, _read
from seulseul.notices.manual_edit import run_interactive as edit
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeRetryError, NoticeService, extract_notice_urls


def run_interactive(
    service: NoticeService,
    *,
    allowed_channels: Collection[str],
    read: Callable[[str], str] = input,
    workspace_id: str | None = None,
    ai_factory: Callable[[], NoticeService | None] | None = None,
    limit: int = 20,
) -> int:
    """메뉴를 한 번 실행하고 저장 결과를 반환한다."""
    try:
        action = _read(read, "공지 관리 [1 등록 / 2 수정 / 3 삭제 / q 취소]: ")
        if action == "1":
            return _add(service, allowed_channels, read, workspace_id, ai_factory)
        if action == "2":
            return edit(service, limit=limit, workspace_id=workspace_id, read=read)
        if action == "3":
            return _delete(service, read, workspace_id, limit)
        print("1, 2, 3 중 하나를 선택하세요.")
        return 1
    except EditCancelled:
        print("\n취소했습니다. 저장하지 않았습니다.")
        return 0
    except KeyboardInterrupt:
        print("\n처리 결과를 확인하지 못했습니다. 목록에서 상태를 확인해 주세요.")
        return 1


def _add(
    service: NoticeService,
    allowed_channels: Collection[str],
    read: Callable[[str], str],
    workspace_id: str | None,
    ai_factory: Callable[[], NoticeService | None] | None,
) -> int:
    workspace_id = workspace_id or _required(read, "워크스페이스 ID")
    source_url = _required(read, "Slack 원문 메시지 링크")
    channel_id, message_ts = parse_slack_permalink(source_url)
    if channel_id not in allowed_channels:
        raise NoticeRetryError("Slack 원문 채널이 자동·수동 공지 채널 설정에 없습니다.")
    text = _multiline(read)
    links = extract_notice_urls(text)
    if not links:
        raise ValueError("원문에 form 또는 docs 링크가 하나 이상 필요합니다.")

    use_ai = _read(read, "AI 분석을 시도할까요? [Y/n]: ").lower() not in {"n", "no"}
    if use_ai and ai_factory is not None:
        try:
            ai_service = ai_factory()
        except (ConfigError, NoticeRetryError) as error:
            print(f"AI 설정을 사용할 수 없어 수동 입력으로 전환합니다: {error}")
            ai_service = None
        if ai_service is not None:
            changed = ai_service.record_channel_message(
                build_manual_event(channel_id, message_ts, text, kind="created"),
                workspace_id=workspace_id,
                source_permalink=source_url,
            )
            if not changed:
                raise NoticeRetryError("공지 원문이 이미 처리되었거나 등록 결과가 없습니다.")
            failed = [notice for notice in changed if notice.processing_status != "processed"]
            if not failed:
                print(
                    "공지 등록과 AI 분석이 완료되었습니다. 실행 중인 봇이 체크리스트를 갱신합니다."
                )
                return 0
            print(f"AI 분석에 실패한 링크 {len(failed)}개를 수동으로 입력합니다.")
            return _manual_update_failed(
                ai_service, channel_id, message_ts, text, source_url, failed, read
            )

    return _manual_add(service, channel_id, message_ts, text, source_url, links, read, workspace_id)


def _manual_update_failed(
    service: NoticeService,
    channel_id: str,
    message_ts: str,
    text: str,
    source_url: str,
    failed,
    read: Callable[[str], str],
) -> int:
    analyses = [(notice, _analysis_input(read, notice.original_url)) for notice in failed]
    if _read(read, "입력한 수동 분석값을 저장할까요? [y/N]: ").lower() != "y":
        print("수동 보완을 취소했습니다. AI 분석 실패 기록은 유지됩니다.")
        return 0
    for notice, analysis in analyses:
        changed = service.record_channel_message(
            build_manual_event(channel_id, message_ts, text, kind="changed"),
            workspace_id=notice.workspace_id,
            source_permalink=source_url,
            manual_analysis=analysis,
            manual_canonical_url=notice.canonical_url,
        )
        if not changed:
            print("원문 상태가 바뀌어 수동 분석을 저장하지 못했습니다.")
            return 1
    print("공지 등록과 수동 분석이 완료되었습니다. 실행 중인 봇이 체크리스트를 갱신합니다.")
    return 0


def _manual_add(
    service: NoticeService,
    channel_id: str,
    message_ts: str,
    text: str,
    source_url: str,
    links: tuple[tuple[str, str], ...],
    read: Callable[[str], str],
    workspace_id: str,
) -> int:
    analyses = []
    for index, (original_url, _) in enumerate(links, 1):
        print(f"\n링크 {index}/{len(links)} 수동 내용: {_display(original_url)}")
        analyses.append(_analysis_input(read, original_url))
    if _read(read, "입력한 공지를 저장할까요? [y/N]: ").lower() != "y":
        print("취소했습니다. 저장하지 않았습니다.")
        return 0
    for index, ((_, canonical_url), analysis) in enumerate(zip(links, analyses, strict=True)):
        kind = "created" if index == 0 else "changed"
        changed = service.record_channel_message(
            build_manual_event(channel_id, message_ts, text, kind=kind),
            workspace_id=workspace_id,
            source_permalink=source_url,
            manual_analysis=analysis,
            manual_canonical_url=canonical_url if len(links) > 1 else None,
        )
        if not changed:
            print("원문 상태가 바뀌어 공지를 저장하지 못했습니다.")
            return 1
    print("공지 등록과 수동 분석이 완료되었습니다. 실행 중인 봇이 체크리스트를 갱신합니다.")
    return 0


def _delete(
    service: NoticeService,
    read: Callable[[str], str],
    workspace_id: str | None,
    limit: int,
) -> int:
    notices = service.recent_notices(limit, workspace_id=workspace_id, configured_only=True)
    if not notices:
        print("설정된 채널에 삭제할 공지가 없습니다.")
        return 0
    print("삭제할 공지를 선택하세요. 같은 원문에 여러 링크가 있으면 모두 삭제됩니다.")
    for index, notice in enumerate(notices, 1):
        title = notice.analysis.title if notice.analysis else "분석 결과 없음"
        print(f"{index}. {_display(title[:80])} [{notice.processing_status}]")
        print(f"   제출 링크: {_display(notice.original_url)}")
        print(f"   원문: {_display(notice.source_permalink) or '링크 없음'}")
    selected = _read(read, f"삭제할 공지 번호 (1~{len(notices)}): ")
    if not selected.isascii() or not selected.isdigit() or not 1 <= int(selected) <= len(notices):
        print("올바른 공지 번호가 아닙니다.")
        return 1
    notice = notices[int(selected) - 1]
    if _read(read, "선택한 Slack 원문과 그 안의 링크를 모두 삭제할까요? [y/N]: ").lower() != "y":
        print("취소했습니다. 저장하지 않았습니다.")
        return 0
    changed = service.record_channel_message(
        build_manual_event(notice.channel_id, notice.message_ts, kind="deleted"),
        workspace_id=notice.workspace_id,
        source_permalink=notice.source_permalink,
    )
    if not changed:
        print("원문이 이미 처리되었거나 삭제할 수 없습니다.")
        return 1
    print("공지 삭제 처리가 완료되었습니다. 실행 중인 봇이 체크리스트를 갱신합니다.")
    return 0


def _analysis_input(read: Callable[[str], str], link: str):
    title = _field(read, f"제목 ({_display(link)})", "", maximum=255)
    summary = _field(read, "요약", "")
    while True:
        deadline = _field(read, "마감일 (YYYY-MM-DD HH:MM)", "", maximum=64)
        try:
            analysis = parse_manual_analysis(
                title=title,
                summary=summary,
                deadline=deadline,
                deadline_source_text=deadline,
            )
        except ValueError as error:
            print(error)
            continue
        source = _field(read, "마감 근거", deadline)
        return replace(analysis, deadline_source_text=source)


def _multiline(read: Callable[[str], str]) -> str:
    print("Slack 원문 내용을 붙여 넣으세요. 입력을 끝내려면 줄 하나에 .done을 입력하세요.")
    lines: list[str] = []
    while True:
        line = _read(read, "원문> ")
        if line == ".done":
            text = "\n".join(lines).strip()
            if not text:
                raise ValueError("공지 원문이 비어 있습니다.")
            if len(text) > MAX_MANUAL_TEXT_CHARS or "\x00" in text:
                raise ValueError(
                    f"공지 원문은 {MAX_MANUAL_TEXT_CHARS}자 이하이며 NUL 문자가 없어야 합니다."
                )
            return text
        lines.append(line)


def _required(read: Callable[[str], str], label: str) -> str:
    return _field(read, label, "")


def _build_ai_factory(repository, resources: ExitStack, allowed_channels):
    def factory() -> NoticeService | None:
        ai_settings = load_ai_settings()
        if ai_settings is None:
            return None
        client = OpenAICompatibleChatClient(
            base_url=ai_settings.base_url,
            api_key=ai_settings.api_key,
            model=ai_settings.model,
            timeout_seconds=ai_settings.timeout_seconds,
            provider=ai_settings.provider,
            disable_thinking=True,
        )
        resources.callback(client.close)
        return NoticeService(
            allowed_channels,
            analyzer=NoticeAnalyzer(client),
            repository=repository,
        )

    return factory


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("조회 개수는 1~100이어야 합니다.") from error
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("조회 개수는 1~100이어야 합니다.")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=_limit, default=20, help="최근 공지 조회 개수 (1~100)")
    parser.add_argument("--workspace-id", help="선택 사항: 특정 워크스페이스만 조회")
    args = parser.parse_args(argv)
    try:
        settings = load_slack_settings()
        allowed_channels = (*settings.notice_channels, *settings.manual_notice_channels)
        engine = create_database_engine(load_database_settings())
        with ExitStack() as resources:
            resources.callback(engine.dispose)
            repository = SqlAlchemyNoticeRepository(create_session_factory(engine))
            service = NoticeService(allowed_channels, repository=repository)
            ai_factory = _build_ai_factory(repository, resources, allowed_channels)
            return run_interactive(
                service,
                allowed_channels=allowed_channels,
                workspace_id=args.workspace_id,
                ai_factory=ai_factory,
                limit=args.limit,
            )
    except (ConfigError, NoticeRetryError, ValueError):
        print(
            "공지 처리 설정·입력값을 확인해 주세요. 비밀값을 포함한 오류 상세는 표시하지 않습니다."
        )
        return 1
    except SQLAlchemyError:
        print("DB 작업에 실패했습니다. PostgreSQL 실행 상태와 DATABASE_URL을 확인해 주세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
