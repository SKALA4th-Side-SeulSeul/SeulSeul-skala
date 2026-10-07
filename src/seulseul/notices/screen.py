"""운영자 터미널 화면에 공통 서식과 표시 폭 계산을 제공한다."""

from __future__ import annotations

from datetime import datetime, timezone
from unicodedata import east_asian_width
from zoneinfo import ZoneInfo

SCREEN_WIDTH = 60
SEOUL = ZoneInfo("Asia/Seoul")
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")


def display_width(value: str) -> int:
    """동아시아 전각·와이드 문자를 두 칸으로 계산한다."""
    return sum(2 if east_asian_width(character) in {"W", "F"} else 1 for character in value)


def pad_display(value: str, width: int) -> str:
    """표시 폭 기준으로 오른쪽에 공백을 채운다."""
    return value + " " * max(0, width - display_width(value))


def truncate_display(value: str, width: int) -> str:
    """표시 폭을 넘는 문자열을 폭 안에서 말줄임표로 자른다."""
    if width <= 0:
        return ""
    if display_width(value) <= width:
        return value
    if width == 1:
        return "…"

    available_width = width - display_width("…")
    result: list[str] = []
    used_width = 0
    for character in value:
        character_width = display_width(character)
        if used_width + character_width > available_width:
            break
        result.append(character)
        used_width += character_width
    return "".join(result) + "…"


def format_seoul_time(value: datetime) -> str:
    """날짜를 Asia/Seoul 기준 MM/DD(요일) HH:MM으로 표시한다."""
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    seoul_value = aware_value.astimezone(SEOUL)
    return f"{seoul_value:%m/%d}({WEEKDAYS[seoul_value.weekday()]}) {seoul_value:%H:%M}"


def format_epoch_seoul_time(value: str) -> str | None:
    """Slack 메시지 시각(초 단위 epoch 문자열)을 서울 시각으로 바꾼다."""
    try:
        moment = datetime.fromtimestamp(float(value), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return format_seoul_time(moment)


def banner(title: str, detail: str, *, width: int = SCREEN_WIDTH) -> list[str]:
    """운영 화면 제목과 보조 정보를 굵은 가로줄 사이에 둔다."""
    rule = "━" * width
    return [rule, f"  {title} · {detail}", rule]


def section_heading(title: str, count: int | None = None) -> str:
    """화면 구획 제목을 만든다."""
    suffix = f" · {count}건" if count is not None else ""
    return f"\n■ {title}{suffix}"


def footer(hints: tuple[str, ...] | list[str], *, width: int = SCREEN_WIDTH) -> list[str]:
    """한두 개의 운영 안내를 연한 가로줄 아래에 둔다."""
    return ["", "─" * width, *(f"  › {hint}" for hint in hints)]
