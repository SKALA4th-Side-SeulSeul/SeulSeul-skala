"""운영자가 공지 처리 상태를 한눈에 확인하는 읽기 전용 대시보드."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.exc import SQLAlchemyError

from seulseul.config import ConfigError, load_database_settings, load_slack_settings
from seulseul.database import create_database_engine, create_session_factory
from seulseul.notices.model import Notice
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.screen import (
    SEOUL,
    WEEKDAYS,
    banner,
    display_width,
    footer,
    format_seoul_time,
    pad_display,
    truncate_display,
)
from seulseul.notices.service import NoticeService

QUERY_LIMIT = 100
TABLE_LIMIT = 10
PANEL_ACTION_LIMIT = 3
PANEL_RECENT_LIMIT = 5
DEFAULT_WIDTH = 80
MIN_WIDTH = 40
MAX_WIDTH = 240
BAR_FILLED = "█"
BAR_EMPTY = "░"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "없음"
    return format_seoul_time(_aware(value))


def _format_deadline(notice: Notice) -> str:
    if notice.analysis is None:
        return "없음"
    return _format_time(notice.analysis.deadline_at)


def _title(notice: Notice) -> str:
    if notice.analysis is None or not notice.analysis.title.strip():
        return "분석 결과 없음"
    return " ".join(notice.analysis.title.split())


def _safe_error(error: str | None) -> str:
    value = (error or "원인 기록 없음").split(", 응답:", 1)[0]
    value = re.sub(r"https?://\S+|(?:nvapi-|xox[baprs]-)[A-Za-z0-9_-]+", "[숨김]", value)
    return " ".join(value.split())[:120]


def _failure_state(notice: Notice, now: datetime) -> tuple[str, str, str]:
    retry_at = notice.next_retry_at
    if retry_at is not None and notice.retry_count < 3:
        schedule = f"다음 재시도 {_format_time(retry_at)} · {notice.retry_count}/3회"
        if _aware(retry_at) <= now:
            schedule = (
                f"재시도 예정 시각 지남 · {_format_time(retry_at)} · {notice.retry_count}/3회"
            )
        return "↻", "자동 재시도 대기", schedule
    return "✖", "수동 조치 필요", f"자동 재시도 {notice.retry_count}/3회 완료"


def _recent_status(notice: Notice) -> tuple[str, str]:
    if notice.processing_status == "processed":
        return "✔", "정상"
    if notice.processing_status == "processing_failed":
        return "✖", "AI 확인 필요"
    return "⚠", "수동 입력 필요"


def _fit_line(value: str, width: int) -> str:
    return truncate_display(value, width)


def _explanation_for_width(width: int, *options: str) -> str:
    """화면 폭에 들어가는 가장 자세한 문장을 고른다. 접두어가 없으면 설명 줄(┆)로 잰다."""
    return next(
        (
            option
            for option in options
            if display_width(option if option.startswith(" ") else f"  ┆ {option}") <= width
        ),
        options[-1],
    )


def _add_section(
    lines: list[str],
    title: str,
    explanation: str,
    *,
    width: int,
    count: int | None = None,
) -> None:
    if lines:
        lines.append("")
    suffix = f" · {count}건" if count is not None else ""
    lines.append(_fit_line(f"■ {title}{suffix}", width))
    lines.append(_fit_line(f"  ┆ {explanation}", width))


def _append_overview(
    lines: list[str], scheduled_count: int, manual_count: int, pending_count: int, width: int
) -> None:
    labels = (
        "↻ 자동 재시도 대기",
        "✖ 수동 조치 필요",
        "⚠ 미적용 원본",
    )
    label_width = max(display_width(label) for label in labels)
    counts = (scheduled_count, manual_count, pending_count)
    for label, count in zip(labels, counts, strict=True):
        lines.append(_fit_line(f"  {pad_display(label, label_width)}  {count}건", width))


def _status_counts(notices: Sequence[Notice], now: datetime) -> tuple[tuple[str, str, int], ...]:
    counts: Counter[str] = Counter()
    for notice in notices:
        if notice.processing_status == "processed":
            counts["processed"] += 1
        elif notice.processing_status == "processing_failed":
            symbol, _, _ = _failure_state(notice, now)
            counts["retry" if symbol == "↻" else "manual"] += 1
        else:
            counts["other"] += 1

    return (
        ("✔", "정상 처리", counts["processed"]),
        ("↻", "자동 재시도 대기", counts["retry"]),
        ("✖", "수동 조치 필요", counts["manual"]),
        ("⚠", "기타 미처리", counts["other"]),
    )


def _bar_lines(
    values: Sequence[tuple[str, str, int]], total: int, width: int, *, show_percent: bool
) -> list[str]:
    label_width = max(display_width(label) for _, label, _ in values)
    count_width = max(1, len(str(max((count for _, _, count in values), default=0))))
    if show_percent:
        count_width = max(count_width, len(str(total)))
        suffix_width = display_width(f" {total:>{count_width}}건 100%")
    else:
        suffix_width = display_width(f" {count_width}건")
    largest_count = max((count for _, _, count in values), default=0)
    lines: list[str] = []

    for symbol, label, count in values:
        prefix = f"  {symbol} {pad_display(label, label_width)}  "
        bar_width = max(1, width - display_width(prefix) - suffix_width)
        if largest_count == 0 or count == 0:
            filled_count = 0
        else:
            filled_count = max(1, round(bar_width * count / largest_count))
        filled_count = min(bar_width, filled_count)
        bar = BAR_FILLED * filled_count + BAR_EMPTY * (bar_width - filled_count)
        if show_percent:
            percentage = round(count * 100 / total) if total else 0
            suffix = f" {count:>{count_width}}건 {percentage:>3}%"
        else:
            suffix = f" {count:>{count_width}}건"
        lines.append(_fit_line(f"{prefix}{bar}{suffix}", width))

    return lines


def _deadline_counts(
    notices: Sequence[Notice], now: datetime
) -> tuple[list[tuple[str, str, int]], int]:
    start_date = now.astimezone(SEOUL).date()
    deadline_counts: Counter[date] = Counter()
    for notice in notices:
        if notice.processing_status != "processed" or notice.analysis is None:
            continue
        deadline = notice.analysis.deadline_at
        if deadline is not None:
            deadline_counts[_aware(deadline).astimezone(SEOUL).date()] += 1

    values: list[tuple[str, str, int]] = []
    for offset in range(7):
        day = start_date + timedelta(days=offset)
        day_label = f"{day:%m/%d}({WEEKDAYS[day.weekday()]})"
        if offset == 0:
            day_label += " 오늘"
        count = deadline_counts[day]
        # 마감 없는 날을 ✔(성공)로, 마감 있는 날을 모두 ⚠(경고)로 읽지 않게 오늘 마감만 강조한다.
        values.append(("⚠" if count and offset == 0 else "•", day_label, count))
    total = sum(count for _, _, count in values)
    return values, total


def _append_truncated_hint(lines: list[str], omitted: int, width: int) -> None:
    hint = f"  … 외 {omitted}건 · 전체 보기: ./view.sh dashboard"
    if display_width(hint) <= width:
        lines.append(hint)
    else:
        lines.extend((f"  … 외 {omitted}건 · 전체 보기:", "    ./view.sh dashboard"))


def _append_action_cards(
    lines: list[str],
    failed: Sequence[Notice],
    now: datetime,
    *,
    width: int,
    panel: bool,
) -> None:
    visible = failed[:PANEL_ACTION_LIMIT] if panel else failed
    for index, notice in enumerate(visible, 1):
        symbol, state, schedule = _failure_state(notice, now)
        lines.append(_fit_line(f"  {index}. {_title(notice)}", width))
        lines.append(_fit_line(f"    상태  {symbol} {state} · {schedule}", width))
        lines.append(_fit_line(f"    마감  {_format_deadline(notice)}", width))
        lines.append(_fit_line(f"    원인  {_safe_error(notice.last_error)}", width))
        if symbol == "✖":
            lines.append(_fit_line(f"    › 다시 분석: ./admin.sh retry {index}", width))
    if panel and len(failed) > len(visible):
        _append_truncated_hint(lines, len(failed) - len(visible), width)


def _append_action_rows(
    lines: list[str], failed: Sequence[Notice], now: datetime, *, width: int
) -> None:
    """관리 콘솔 패널용: 조치가 필요한 공지를 한 줄씩 표로 보여 준다."""
    visible = failed[:PANEL_ACTION_LIMIT]
    states = [_failure_state(notice, now) for notice in visible]
    number_width = len(str(len(visible)))
    state_width = max(display_width(f"{symbol} {state}") for symbol, state, _ in states)
    deadline_width = display_width("00/00(수) 00:00")
    title_width = max(1, width - (2 + number_width + 2 + state_width + 2 + 2 + deadline_width))
    for index, (notice, (symbol, state, _)) in enumerate(zip(visible, states, strict=True), 1):
        title = pad_display(truncate_display(_title(notice), title_width), title_width)
        lines.append(
            f"  {index:>{number_width}}  {pad_display(f'{symbol} {state}', state_width)}  "
            f"{title}  {_format_deadline(notice)}"
        )
    if len(failed) > len(visible):
        _append_truncated_hint(lines, len(failed) - len(visible), width)
    if any(symbol == "✖" for symbol, _, _ in states):
        lines.append(
            _explanation_for_width(
                width,
                "  › ✖ 공지 다시 분석: 관리 콘솔 메뉴 1 또는 ./admin.sh retry 번호",
                "  › ✖ 다시 분석: 메뉴 1 또는 ./admin.sh retry 번호",
                "  › 다시 분석: 메뉴 1",
            )
        )


def _append_recent_table(
    lines: list[str], recent: Sequence[Notice], *, width: int, panel: bool
) -> None:
    visible = recent[:PANEL_RECENT_LIMIT] if panel else recent[:TABLE_LIMIT]
    statuses = [_recent_status(notice) for notice in visible]
    status_width = max(
        (display_width(f"{symbol} {label}") for symbol, label in statuses),
        default=display_width("상태"),
    )
    status_width = max(status_width, display_width("상태"))
    deadline_width = display_width("00/00(수) 00:00")
    title_width = max(1, width - (2 + status_width + 2 + 2 + deadline_width))
    title_header = pad_display(truncate_display("공지 제목", title_width), title_width)
    lines.append(
        _fit_line(
            f"  {pad_display('상태', status_width)}  {title_header}  "
            f"{pad_display('마감', deadline_width)}",
            width,
        )
    )
    for notice, (symbol, status) in zip(visible, statuses, strict=True):
        title = truncate_display(_title(notice), title_width)
        deadline = pad_display(_format_deadline(notice), deadline_width)
        lines.append(
            _fit_line(
                f"  {pad_display(f'{symbol} {status}', status_width)}  "
                f"{pad_display(title, title_width)}  {deadline}",
                width,
            )
        )
    if panel and len(recent) > len(visible):
        _append_truncated_hint(lines, len(recent) - len(visible), width)


def render_dashboard(
    failed: Sequence[Notice],
    pending_sources: Sequence[tuple[str, str, str]],
    recent: Sequence[Notice],
    *,
    now: datetime | None = None,
    width: int = DEFAULT_WIDTH,
    panel: bool = False,
) -> str:
    """폭에 맞춰 공지 요약·조치 카드·최근 상태를 출력한다."""
    if not MIN_WIDTH <= width <= MAX_WIDTH:
        raise ValueError(f"width는 {MIN_WIDTH}~{MAX_WIDTH} 사이여야 합니다.")
    now = _aware(now or datetime.now(timezone.utc))
    scheduled = [
        notice for notice in failed if notice.next_retry_at is not None and notice.retry_count < 3
    ]
    manual_count = len(failed) - len(scheduled)
    table_limit = PANEL_RECENT_LIMIT if panel else TABLE_LIMIT
    table_recent = recent[:table_limit]
    lines: list[str] = []

    if not panel:
        lines.extend(banner("슬슬 운영 대시보드", f"{_format_time(now)} 기준", width=width))
        lines[1] = _fit_line(lines[1], width)

    _add_section(
        lines,
        "한눈에 보기",
        _explanation_for_width(
            width,
            "↻는 봇이 스스로 다시 시도, ✖는 사람이 처리(메뉴 1·2), ⚠는 아직 반영 전인 원본입니다.",
            "↻ 봇이 재시도 · ✖ 사람이 처리(메뉴 1·2) · ⚠ 반영 전 원본",
            "↻ 자동 · ✖ 직접 처리 · ⚠ 반영 전",
        ),
        width=width,
    )
    _append_overview(lines, len(scheduled), manual_count, len(pending_sources), width)

    _add_section(
        lines,
        "처리가 필요한 공지",
        _explanation_for_width(
            width,
            "✖는 메뉴 1로 AI 재분석하거나 메뉴 2로 직접 고치고, ↻는 예약 시각까지 기다리면 됩니다.",
            "✖ 메뉴 1 재분석·메뉴 2 직접 수정 · ↻ 예약 시각까지 대기",
            "✖ 메뉴 1·2로 처리 · ↻ 대기",
        ),
        width=width,
        count=len(failed),
    )
    if not failed:
        lines.append("  ✔ 지금 처리할 공지가 없습니다.")
    elif panel:
        _append_action_rows(lines, failed, now, width=width)
    else:
        _append_action_cards(lines, failed, now, width=width, panel=panel)

    _add_section(
        lines,
        f"공지 상태 분포 · 최근 {len(recent)}건",
        _explanation_for_width(
            width,
            "최근 공지의 상태를 비율로 보여 줍니다. ✖ 막대가 길면 손볼 공지가 많습니다.",
            "최근 공지의 상태 비율입니다. ✖ 막대가 길면 손볼 공지가 많습니다.",
            "최근 공지의 상태 비율입니다.",
        ),
        width=width,
    )
    lines.extend(_bar_lines(_status_counts(recent, now), len(recent), width, show_percent=True))

    _add_section(
        lines,
        "다가오는 마감 · 7일",
        _explanation_for_width(
            width,
            "학생 체크리스트에 걸린 마감이 날짜별로 몇 건인지 보여 줍니다. ⚠는 오늘 마감입니다.",
            "날짜별 체크리스트 마감 건수입니다. ⚠는 오늘 마감입니다.",
            "날짜별 마감 건수입니다.",
        ),
        width=width,
    )
    deadlines, deadline_total = _deadline_counts(recent, now)
    if deadline_total == 0:
        lines.append("  ✔ 7일 안에 마감되는 공지가 없습니다.")
    else:
        lines.extend(_bar_lines(deadlines, deadline_total, width, show_percent=False))

    _add_section(
        lines,
        "최근 공지",
        _explanation_for_width(
            width,
            "가장 최근에 저장된 공지와 분석 상태·마감입니다.",
            "최근 공지의 상태와 마감입니다.",
        ),
        width=width,
        count=len(table_recent),
    )
    if not table_recent:
        lines.append("  • 저장된 공지가 없습니다.")
    else:
        _append_recent_table(lines, recent, width=width, panel=panel)

    if not panel:
        lines.extend(
            footer(("원본: ./admin.sh retry pending", "상세: ./view.sh dashboard"), width=width)
        )
    return "\n".join(_fit_line(line, width) for line in lines)


def _terminal_width(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("40~240 사이의 정수를 입력하세요.") from error
    if not MIN_WIDTH <= value <= MAX_WIDTH:
        raise argparse.ArgumentTypeError("40~240 사이의 정수를 입력하세요.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=_terminal_width, default=DEFAULT_WIDTH)
    parser.add_argument("--panel", action="store_true", help="관리 콘솔 안에 넣는 간결한 화면")
    args = parser.parse_args(argv)

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
                    service.recent_notices(QUERY_LIMIT, configured_only=True),
                    width=args.width,
                    panel=args.panel,
                )
            )
        finally:
            engine.dispose()
    except ConfigError:
        print(
            "\n".join(
                _fit_line(line, args.width)
                for line in (
                    "✖ 운영 설정을 확인할 수 없습니다.",
                    "  › 서버 운영 설정 값을 점검한 뒤 다시 확인하세요.",
                )
            )
        )
        return 1
    except SQLAlchemyError:
        print(
            "\n".join(
                _fit_line(line, args.width)
                for line in (
                    "✖ DB를 조회하지 못했습니다.",
                    "  › ./view.sh 에서 서비스 상태를 확인하세요.",
                )
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
