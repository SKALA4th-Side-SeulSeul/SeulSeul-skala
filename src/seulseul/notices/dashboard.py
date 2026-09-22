"""운영자가 공지 처리 상태를 한눈에 확인하는 읽기 전용 대시보드."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from seulseul.config import ConfigError, load_database_settings, load_slack_settings
from seulseul.database import create_database_engine, create_session_factory
from seulseul.notices.model import Notice
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeService

SEOUL = ZoneInfo("Asia/Seoul")
QUERY_LIMIT = 100


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "없음"
    return _aware(value).astimezone(SEOUL).strftime("%m/%d %H:%M")


def _format_deadline(notice: Notice) -> str:
    if notice.analysis is None:
        return "마감 없음"
    return f"마감 {_format_time(notice.analysis.deadline_at)}"


def _title(notice: Notice) -> str:
    if notice.analysis is None or not notice.analysis.title.strip():
        return "분석 결과 없음"
    return " ".join(notice.analysis.title.split())[:80]


def _safe_error(error: str | None) -> str:
    value = (error or "원인 기록 없음").split(", 응답:", 1)[0]
    value = re.sub(r"https?://\S+|(?:nvapi-|xox[baprs]-)[A-Za-z0-9_-]+", "[숨김]", value)
    return " ".join(value.split())[:120]


def _failure_state(notice: Notice, now: datetime) -> tuple[str, str]:
    retry_at = notice.next_retry_at
    if retry_at is not None and notice.retry_count < 3:
        schedule = (
            "지금 재시도 대상" if _aware(retry_at) <= now else f"다음 {_format_time(retry_at)}"
        )
        return "자동 재시도 대기", f"{schedule} · {notice.retry_count}/3회"
    return "수동 조치 필요", f"자동 재시도 {notice.retry_count}/3회 완료"


def _recent_status(notice: Notice) -> str:
    if notice.processing_status == "processed":
        return "정상"
    if notice.processing_status == "processing_failed":
        return "AI 확인 필요"
    return "수동 입력 필요"


def render_dashboard(
    failed: Sequence[Notice],
    pending_sources: Sequence[tuple[str, str, str]],
    recent: Sequence[Notice],
    *,
    now: datetime | None = None,
) -> str:
    """공지 상태를 관리자에게 필요한 요약만 포함해 출력한다."""
    now = _aware(now or datetime.now(timezone.utc))
    scheduled = [
        notice for notice in failed if notice.next_retry_at is not None and notice.retry_count < 3
    ]
    manual_count = len(failed) - len(scheduled)
    lines = [
        "SeulSeul 운영 대시보드",
        f"조회 시각: {_format_time(now)}",
        "",
        "공지 처리 현황",
        f"  자동 재시도 대기: {len(scheduled)}건",
        f"  수동 조치 필요: {manual_count}건",
        f"  미적용 원본: {len(pending_sources)}건",
        "",
        "처리 필요 목록",
    ]
    if not failed:
        lines.append("  현재 수동 조치가 필요한 공지가 없습니다.")
    else:
        for index, notice in enumerate(failed, 1):
            state, schedule = _failure_state(notice, now)
            lines.append(f"  {index}. {_title(notice)} · {state} · {_format_deadline(notice)}")
            lines.append(f"     {_safe_error(notice.last_error)} · {schedule}")

    lines.extend(("", "최근 공지"))
    if not recent:
        lines.append("  저장된 공지가 없습니다.")
    else:
        for notice in recent:
            lines.append(
                f"  [{_recent_status(notice)}] {_title(notice)} · {_format_deadline(notice)}"
            )
    lines.extend(("", "관리 명령: ./admin.sh · 상세 로그: ./view.sh logs bot"))
    return "\n".join(lines)


def main() -> int:
    try:
        settings = load_slack_settings()
        channels = (*settings.notice_channels, *settings.manual_notice_channels)
        engine = create_database_engine(load_database_settings())
        try:
            service = NoticeService(
                channels,
                repository=SqlAlchemyNoticeRepository(create_session_factory(engine)),
            )
            print(
                render_dashboard(
                    service.failed_notices(QUERY_LIMIT),
                    service.pending_sources(QUERY_LIMIT),
                    service.recent_notices(10, configured_only=True),
                )
            )
        finally:
            engine.dispose()
    except ConfigError:
        print("운영 설정을 확인할 수 없습니다. ./view.sh config를 먼저 확인하세요.")
        return 1
    except SQLAlchemyError:
        print("DB를 조회하지 못했습니다. PostgreSQL 실행 상태를 확인하세요.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
