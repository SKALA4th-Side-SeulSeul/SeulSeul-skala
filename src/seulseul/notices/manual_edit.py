"""저장된 공지를 번호로 선택해 분석 결과만 수정하는 운영자 입력 도우미."""

import argparse
from collections.abc import Callable
from dataclasses import replace
from datetime import timezone

from sqlalchemy.exc import SQLAlchemyError

from seulseul.config import ConfigError, load_database_settings, load_slack_settings
from seulseul.database import create_database_engine, create_session_factory
from seulseul.notices.manual import (
    MAX_MANUAL_TEXT_CHARS,
    SEOUL,
    build_manual_event,
    parse_manual_analysis,
    parse_slack_permalink,
)
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeRetryError, NoticeService, extract_notice_urls


class EditCancelled(Exception):
    """DB 저장을 시작하기 전에 사용자가 입력을 취소했다."""


def _display(value: str) -> str:
    # DB 원문·입력값의 터미널 제어 문자가 안내나 확인 화면을 덮어쓰지 않게 한다.
    return "".join(char if char.isprintable() else f"\\u{ord(char):04x}" for char in value)


def _read(read: Callable[[str], str], prompt: str) -> str:
    try:
        value = read(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise EditCancelled from None
    if value.lower() == "q":
        raise EditCancelled
    return value


def _field(read: Callable[[str], str], label: str, default: str, *, maximum: int = 12_000) -> str:
    while True:
        value = _read(read, f"{label} [{_display(default) or '필수 입력'}]: ") or default
        if not value or len(value) > maximum or "\x00" in value:
            print(
                f"비어 있지 않은 {maximum}자 이하의 값을 입력하세요. NUL 문자는 허용하지 않습니다."
            )
            continue
        return value


def run_interactive(
    service: NoticeService,
    *,
    limit: int = 20,
    workspace_id: str | None = None,
    read: Callable[[str], str] = input,
) -> int:
    """확인 전에는 읽기만 수행하며 EOF·Ctrl+C·q는 저장 없이 종료한다."""
    try:
        return _edit(service, limit, workspace_id, read)
    except EditCancelled:
        print("\n취소했습니다. 저장하지 않았습니다.")
        return 0
    except KeyboardInterrupt:
        print("\n처리 결과를 확인하지 못했습니다. 다시 목록에서 저장 상태를 확인해 주세요.")
        return 1


def _edit(service, limit, workspace_id, read) -> int:
    notices = service.recent_notices(limit, workspace_id=workspace_id, configured_only=True)
    if not notices:
        print("설정된 채널에 수정할 공지가 없습니다. 새 등록은 manual add를 사용하세요.")
        return 0
    print("공지 수동 수정 · 번호 선택 → 값 입력 → 확인 (q: 취소)")
    for index, notice in enumerate(notices, 1):
        title = notice.analysis.title if notice.analysis else "분석 결과 없음"
        print(f"{index}. {_display(title[:80])} [{notice.processing_status}]")
        print(f"   {_display(notice.workspace_id)} / {_display(notice.channel_id)}")
        print(f"   {_display(notice.source_permalink) or '원문 링크 없음'}")
    if len(notices) == limit:
        print("최근 조회 한도입니다. 더 보려면 ./notice_edit.sh --limit 100을 사용하세요.")
    while True:
        selected = _read(read, "수정할 공지 번호: ")
        if selected.isascii() and selected.isdigit() and 1 <= int(selected) <= len(notices):
            original = notices[int(selected) - 1]
            break
        print(f"1~{len(notices)} 사이의 번호를 입력하세요.")

    links = extract_notice_urls(original.text)
    if len(links) != 1 or links[0][1] != original.canonical_url:
        print("처리 대상 링크가 하나인 원문만 수동 수정할 수 있습니다. 저장하지 않았습니다.")
        return 1
    if len(original.text) > MAX_MANUAL_TEXT_CHARS or "\x00" in original.text:
        print("원문이 저장 규격을 벗어났습니다. 원문을 확인해 주세요. 저장하지 않았습니다.")
        return 1
    permalink = original.source_permalink
    while True:
        if not permalink:
            permalink = _read(read, "Slack 원문 메시지 링크: ")
        try:
            if parse_slack_permalink(permalink) != (original.channel_id, original.message_ts):
                raise ValueError("선택한 공지의 원문 메시지 링크가 아닙니다.")
            break
        except ValueError as error:
            print(error)
            permalink = ""

    previous = original.analysis
    if previous:
        deadline = previous.deadline_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        previous = replace(previous, deadline_at=deadline.astimezone(SEOUL))
    old = {
        "title": previous.title if previous else "",
        "summary": previous.summary if previous else "",
        "deadline": previous.deadline_at.isoformat(sep=" ") if previous else "",
        "deadline_source_text": previous.deadline_source_text if previous else "",
    }
    print(f"\n선택한 원문: {_display(permalink)}")
    print(f"제출 링크: {_display(original.original_url)}")
    print("원문·링크·완료 기록은 보존합니다. Enter는 기존 값 유지, 시간은 한국 시간입니다.")
    title = _field(read, "제목", old["title"], maximum=255)
    summary = _field(read, "요약", old["summary"])
    while True:
        deadline = _field(read, "마감일 (YYYY-MM-DD HH:MM)", old["deadline"], maximum=64)
        try:
            analysis = parse_manual_analysis(
                title=title,
                summary=summary,
                deadline=deadline,
                deadline_source_text=deadline,
            )
            break
        except ValueError as error:
            print(error)
    same_deadline = previous and analysis.deadline_at == previous.deadline_at
    source = _field(
        read,
        "마감 근거",
        old["deadline_source_text"] if same_deadline else deadline,
    )
    analysis = replace(analysis, deadline_source_text=source)
    if (
        previous == analysis
        and original.processing_status == "processed"
        and permalink == original.source_permalink
    ):
        print("변경된 내용이 없습니다. 저장하지 않았습니다.")
        return 0
    print("\n변경 전 → 변경 후")
    for label, before, after in (
        ("제목", old["title"], title),
        ("요약", old["summary"], summary),
        ("마감일", old["deadline"], analysis.deadline_at.isoformat(sep=" ")),
        ("마감 근거", old["deadline_source_text"], source),
    ):
        print(f"{label}: {_display(before) or '(없음)'} → {_display(after)}")
    print("AI 호출 없이 저장합니다. 마감이 지난 항목은 학생 목록에 표시되지 않습니다.")
    if _read(read, "이 내용으로 저장할까요? [y/N]: ").lower() != "y":
        print("취소했습니다. 저장하지 않았습니다.")
        return 0
    changed = service.record_channel_message(
        build_manual_event(original.channel_id, original.message_ts, original.text, kind="changed"),
        workspace_id=original.workspace_id,
        source_permalink=permalink,
        manual_analysis=analysis,
        expected_notice=original,
    )
    if not changed:
        print(
            "원문이 변경·삭제되었거나 처리 중입니다. 저장하지 않았습니다. 목록을 다시 확인하세요."
        )
        return 1
    print("저장했습니다. 실행 중인 운영 봇이 기존 체크리스트 DM을 갱신합니다.")
    return 0


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
        engine = create_database_engine(load_database_settings())
        try:
            service = NoticeService(
                (*settings.notice_channels, *settings.manual_notice_channels),
                repository=SqlAlchemyNoticeRepository(create_session_factory(engine)),
            )
            return run_interactive(service, limit=args.limit, workspace_id=args.workspace_id)
        finally:
            engine.dispose()
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
