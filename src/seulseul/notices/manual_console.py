"""운영자 공지 등록·수정·삭제를 한 번에 수행하는 대화형 도우미.

봇이 들어가 있지 않거나 설정에 없는 채널의 원문도 등록할 수 있다. 이때 학생 배정은
설정된 공지 채널 하나를 "배정 기준 채널"로 골라 그 채널의 대상(광주 전체·N반)을 따르고,
원문 링크는 실제 Slack 메시지 링크를 그대로 저장한다(D-042, DB 구조 변경 없음).
실패하면 작업·단계·원인·해결 방법을 화면에 보여 주고 같은 내용을 로그(표준 오류)로 남긴다.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Callable, Collection, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace

from sqlalchemy.exc import SQLAlchemyError

from seulseul.ai.client import OpenAICompatibleChatClient
from seulseul.ai.service import NoticeAnalyzer
from seulseul.checklists.model import ChecklistDeliveryError
from seulseul.config import (
    ConfigError,
    load_ai_settings,
    load_database_settings,
    load_notice_targets,
    load_slack_settings,
)
from seulseul.database import create_database_engine, create_session_factory
from seulseul.logging_setup import configure_logging
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
from seulseul.slack.client import SlackChecklistClient

logger = logging.getLogger(__name__)
# 화면·로그에 남기는 실패 원인에서 URL과 토큰 형태의 값을 가린다.
SECRET_PATTERN = re.compile(r"https?://\S+|(?:nvapi-|xox[baprs]-)[A-Za-z0-9_-]+")
# AI 호출 라이브러리의 요청 단위 INFO 로그가 운영자 화면을 덮지 않게 한다.
QUIET_LOGGERS = ("httpx", "httpcore", "openai", "urllib3")


@dataclass
class Progress:
    """실패 안내와 로그에 쓰는 현재 작업과 단계."""

    action: str = "준비"
    stage: str = "설정 읽기"


def run_interactive(
    service: NoticeService,
    *,
    allowed_channels: Collection[str],
    read: Callable[[str], str] = input,
    workspace_id: str | None = None,
    ai_factory: Callable[[], NoticeService | None] | None = None,
    limit: int = 20,
    channel_targets: Mapping[str, int | None] | None = None,
    workspace_resolver: Callable[[], str] | None = None,
    progress: Progress | None = None,
) -> int:
    """메뉴를 한 번 실행하고 저장 결과를 반환한다."""
    progress = progress or Progress()
    progress.action, progress.stage = "메뉴", "작업 선택"
    try:
        action = _read(read, "공지 관리 [1 등록 / 2 수정 / 3 삭제 / q 취소]: ")
        if action == "1":
            progress.action = "등록"
            result = _add(
                service,
                allowed_channels,
                read,
                workspace_id,
                ai_factory,
                channel_targets=channel_targets,
                workspace_resolver=workspace_resolver,
                progress=progress,
            )
        elif action == "2":
            progress.action, progress.stage = "수정", "공지 선택·값 입력·저장"
            result = edit(service, limit=limit, workspace_id=workspace_id, read=read)
        elif action == "3":
            progress.action, progress.stage = "삭제", "공지 선택·삭제"
            result = _delete(service, read, workspace_id, limit)
        else:
            print("1, 2, 3 중 하나를 선택하세요.")
            return 1
    except EditCancelled:
        print("\n취소했습니다. 저장하지 않았습니다.")
        return 0
    except KeyboardInterrupt:
        print("\n처리 결과를 확인하지 못했습니다. 목록에서 상태를 확인해 주세요.")
        logger.warning(
            "수동 공지 처리 중단: action=%s stage=%s reason=입력 중 Ctrl+C",
            progress.action,
            progress.stage,
        )
        return 1
    if result != 0:
        # 화면에 이유를 이미 출력한 저장 거절(원문 변경·링크 없음 등)도 로그로 추적한다.
        logger.warning(
            "수동 공지 처리 중단: action=%s stage=%s reason=저장하지 않음(화면 안내 참고)",
            progress.action,
            progress.stage,
        )
    return result


def _target_label(channel_id: str, channel_targets: Mapping[str, int | None] | None) -> str:
    if channel_targets is None or channel_id not in channel_targets:
        return "배정 대상 미확인"
    target = channel_targets[channel_id]
    return "광주 전체" if target is None else f"{target}반"


def _resolve_workspace(read: Callable[[str], str], resolver: Callable[[], str] | None) -> str:
    """봇 토큰의 워크스페이스를 자동으로 쓰고, 확인하지 못할 때만 입력을 받는다.

    체크리스트 배정도 봇 토큰의 워크스페이스 기준이라 같은 값을 써야 학생에게 보인다.
    """
    if resolver is not None:
        try:
            workspace_id = resolver()
        except ChecklistDeliveryError as error:
            print(f"⚠ 워크스페이스를 자동으로 확인하지 못했습니다({error.code}). 직접 입력하세요.")
            logger.warning("워크스페이스 자동 확인 실패: code=%s", error.code)
        else:
            print(f"• 워크스페이스 {_display(workspace_id)} (봇 토큰 기준 자동 확인)")
            return workspace_id
    return _required(read, "워크스페이스 ID (T로 시작)")


def _choose_target_channel(
    read: Callable[[str], str],
    source_channel: str,
    allowed_channels: Collection[str],
    channel_targets: Mapping[str, int | None] | None,
) -> str:
    """학생 배정 기준 채널을 정한다. 원문 채널이 설정에 있으면 묻지 않는다."""
    if source_channel in allowed_channels:
        label = _target_label(source_channel, channel_targets)
        print(f"• 배정 대상: {label} (원문 채널 설정)")
        return source_channel
    channels = tuple(dict.fromkeys(allowed_channels))
    if not channels:
        raise NoticeRetryError("배정 기준으로 쓸 공지 채널 설정이 없습니다.")
    print("⚠ 봇이 없거나 설정에 없는 채널의 원문입니다. 원문 링크는 그대로 저장합니다.")
    print("  학생 배정 기준이 될 채널을 고르세요. 그 채널의 대상 학생에게 공지가 보입니다.")
    for index, channel in enumerate(channels, 1):
        print(f"  {index}) {_target_label(channel, channel_targets)} · {_display(channel)}")
    while True:
        value = _read(read, f"배정 기준 채널 번호 (1~{len(channels)}, q 취소): ")
        if value.isascii() and value.isdigit() and 1 <= int(value) <= len(channels):
            channel = channels[int(value) - 1]
            break
        if value in channels:
            channel = value
            break
        print(f"1~{len(channels)} 사이의 번호나 위 목록의 채널 ID를 입력하세요.")
    print(f"• 배정 대상: {_target_label(channel, channel_targets)}")
    return channel


def _log_done(progress: Progress, **details: object) -> None:
    fields = " ".join(f"{key}={value}" for key, value in details.items())
    logger.info("수동 공지 처리 완료: action=%s %s", progress.action, fields)


def _add(
    service: NoticeService,
    allowed_channels: Collection[str],
    read: Callable[[str], str],
    workspace_id: str | None,
    ai_factory: Callable[[], NoticeService | None] | None,
    *,
    channel_targets: Mapping[str, int | None] | None = None,
    workspace_resolver: Callable[[], str] | None = None,
    progress: Progress | None = None,
) -> int:
    progress = progress or Progress(action="등록")
    progress.stage = "워크스페이스 확인"
    workspace_id = workspace_id or _resolve_workspace(read, workspace_resolver)
    progress.stage = "원문 링크 확인"
    source_url = _required(read, "Slack 원문 메시지 링크")
    source_channel, message_ts = parse_slack_permalink(source_url)
    progress.stage = "배정 채널 선택"
    channel_id = _choose_target_channel(read, source_channel, allowed_channels, channel_targets)
    target = _target_label(channel_id, channel_targets)
    progress.stage = "원문 입력"
    text = _multiline(read)
    links = extract_notice_urls(text)
    if not links:
        raise ValueError("원문에 form 또는 docs 링크가 하나 이상 필요합니다.")

    progress.stage = "AI 분석"
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
                _log_done(progress, target=target, links=len(changed), analysis="AI")
                return 0
            print(f"AI 분석에 실패한 링크 {len(failed)}개를 수동으로 입력합니다.")
            logger.warning(
                "수동 공지 AI 분석 실패: links=%d reason=%s",
                len(failed),
                _safe_reason(failed[0].last_error or "원인 기록 없음"),
            )
            progress.stage = "AI 실패 링크 수동 입력·저장"
            result = _manual_update_failed(
                ai_service, channel_id, message_ts, text, source_url, failed, read
            )
            if result == 0:
                _log_done(progress, target=target, links=len(changed), analysis="AI+수동")
            return result

    progress.stage = "수동 분석 입력·저장"
    result = _manual_add(
        service, channel_id, message_ts, text, source_url, links, read, workspace_id
    )
    if result == 0:
        _log_done(progress, target=target, links=len(links), analysis="수동")
    return result


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
    logger.info("수동 공지 처리 완료: action=삭제 links=%d", len(changed))
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


def _safe_reason(error: BaseException | str) -> str:
    text = str(error).strip() or (type(error).__name__ if isinstance(error, BaseException) else "")
    return " ".join(SECRET_PATTERN.sub("[숨김]", text).split())[:200] or "원인 기록 없음"


def _failure_hint(progress: Progress, error: BaseException) -> str:
    if isinstance(error, ConfigError):
        return "운영 .env의 Slack·DB·공지 채널 설정을 확인한 뒤 ./run.sh 로 다시 시작하세요."
    hints = {
        "워크스페이스 확인": "T로 시작하는 워크스페이스 ID를 입력하거나 --workspace-id로 지정",
        "원문 링크 확인": (
            "Slack 메시지 ⋮ 메뉴의 '링크 복사' 주소"
            "(https://<워크스페이스>.slack.com/archives/C…/p…)를 그대로 붙여 넣으세요."
        ),
        "배정 채널 선택": "SLACK_NOTICE_CHANNELS·SLACK_NOTICE_TARGETS 설정을 확인하세요.",
        "원문 입력": "원문에 form·docs 제출 링크가 있는지, 길이 제한을 넘지 않았는지 확인하세요.",
    }
    return hints.get(
        progress.stage,
        "같은 원문이 이미 등록됐을 수 있습니다. 목록에서 확인하고 필요하면 2 수정을 사용하세요.",
    )


def _report_failure(
    progress: Progress, error: BaseException, *, hint: str, reason: str | None = None
) -> int:
    reason = reason or _safe_reason(error)
    print()
    print("✖ 수동 공지 처리에 실패했습니다. 실패한 단계부터는 저장되지 않았습니다.")
    print(f"  작업  {progress.action}")
    print(f"  단계  {progress.stage}")
    print(f"  원인  {reason}")
    print(f"  › {hint}")
    print("  › 실패 기록 보기: ./view.sh logs manual")
    logger.error(
        "수동 공지 처리 실패: action=%s stage=%s error=%s reason=%s",
        progress.action,
        progress.stage,
        type(error).__name__,
        reason,
    )
    return 1


def _load_targets(settings) -> Mapping[str, int | None] | None:
    try:
        return load_notice_targets(
            settings.notice_channels, manual_notice_channels=settings.manual_notice_channels
        )
    except ConfigError as error:
        # 대상 이름은 안내용이다. 설정을 읽지 못해도 등록 자체는 막지 않는다.
        logger.warning("공지 채널 대상 설정을 읽지 못했습니다: %s", _safe_reason(error))
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=_limit, default=20, help="최근 공지 조회 개수 (1~100)")
    parser.add_argument(
        "--workspace-id", help="선택 사항: 워크스페이스 지정 (없으면 봇 토큰 기준 자동 확인)"
    )
    args = parser.parse_args(argv)
    configure_logging()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    progress = Progress()
    try:
        settings = load_slack_settings()
        allowed_channels = (*settings.notice_channels, *settings.manual_notice_channels)
        channel_targets = _load_targets(settings)
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
                channel_targets=channel_targets,
                workspace_resolver=lambda: SlackChecklistClient.from_token(
                    settings.bot_token
                ).workspace_id(),
                progress=progress,
            )
    except (ConfigError, NoticeRetryError, ValueError) as error:
        return _report_failure(progress, error, hint=_failure_hint(progress, error))
    except SQLAlchemyError as error:
        # DB 오류 문자열에는 접속 정보가 섞일 수 있어 오류 종류만 보여 준다.
        return _report_failure(
            progress,
            error,
            reason=f"DB 작업에 실패했습니다({type(error).__name__}).",
            hint="PostgreSQL이 실행 중인지 ./view.sh 로 확인하고, 계속되면 ./view.sh logs postgres",
        )
    except Exception as error:
        # 예상하지 못한 오류도 원인과 위치를 기록해 두고 콘솔은 정상적으로 끝낸다.
        logger.exception("수동 공지 처리 중 예상하지 못한 오류")
        return _report_failure(
            progress, error, hint="실패 기록(./view.sh logs manual)을 개발자에게 전달하세요."
        )


if __name__ == "__main__":
    raise SystemExit(main())
