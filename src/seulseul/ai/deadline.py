"""AI가 인용한 마감 표현을 독립적으로 계산한다. 불명확하면 추측하지 않는다."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from seulseul.ai.client import AiClientError

SEOUL = ZoneInfo("Asia/Seoul")
DATE = re.compile(
    r"(?<!\d)(?:(?P<year>\d{4})\s*(?:년\s*|[-/.]))?"
    r"(?P<month>\d{1,2})\s*(?:월\s*|[-/.])(?P<day>\d{1,2})(?:\s*일)?(?!\d)"
)
RELATIVE = re.compile(r"오늘|내일|모레|(?:이번|다음)\s*주\s*[월화수목금토일]요일")
CLOCK = re.compile(
    r"(?<!\d)(?:(?P<period>오전|오후)\s*)?(?P<hour>\d{1,2})"
    r"(?:\s*:\s*(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
    r"|\s*시(?:\s*(?P<ko_minute>\d{1,2})\s*분)?)(?!\d)"
)


def expected_deadline(evidence: str, posted_at: datetime) -> datetime:
    """명시 날짜/오늘·내일·모레/이번·다음 주 요일, 24시간·오전/오후 시각 지원."""
    posted = posted_at.astimezone(SEOUL)
    days: set[date] = set()
    try:
        for match in DATE.finditer(evidence):
            days.add(
                date(int(match["year"] or posted.year), int(match["month"]), int(match["day"]))
            )
        for match in RELATIVE.finditer(evidence):
            phrase = re.sub(r"\s", "", match[0])
            if phrase in {"오늘", "내일", "모레"}:
                days.add(posted.date() + timedelta(days={"오늘": 0, "내일": 1, "모레": 2}[phrase]))
            else:
                offset = "월화수목금토일".index(phrase[-3]) - posted.weekday()
                days.add(
                    posted.date() + timedelta(days=offset + (7 if phrase.startswith("다음") else 0))
                )
    except (ValueError, OverflowError) as error:
        raise AiClientError("원문의 마감 날짜가 유효하지 않습니다.") from error
    if len(days) != 1:
        raise AiClientError(
            "마감 표현에서 날짜 하나를 확정할 수 없습니다. 명시적인 날짜가 필요합니다."
        )

    times: set[tuple[int, int, int]] = set()
    for match in CLOCK.finditer(evidence):
        hour = int(match["hour"])
        minute = int(match["minute"] or match["ko_minute"] or 0)
        second = int(match["second"] or 0)
        if match["period"]:
            if not 1 <= hour <= 12:
                raise AiClientError("원문의 오전/오후 마감 시각이 유효하지 않습니다.")
            hour = hour % 12 + (12 if match["period"] == "오후" else 0)
        if hour > 24 or minute > 59 or second > 59 or (hour == 24 and (minute or second)):
            raise AiClientError("원문의 마감 시각이 유효하지 않습니다.")
        times.add((hour, minute, second))
    if re.search(r"(?:시\s*반|밤|저녁|아침|새벽)", evidence):
        raise AiClientError("마감 시각을 오전/오후 시·분 또는 HH:MM으로 명시해 주세요.")
    if "자정" in evidence:
        # '28일 자정'은 시작/끝 중 어느 쪽인지 모호하다.
        raise AiClientError("자정은 날짜 경계가 모호합니다. 00:00 또는 24:00으로 적어 주세요.")
    if "정오" in evidence:
        times.add((12, 0, 0))
    if len(times) > 1:
        raise AiClientError("마감 표현에 시각이 여러 개 있습니다.")
    if re.search(r"UTC|GMT|[+-]\d{2}:\d{2}", evidence, re.IGNORECASE):
        raise AiClientError("마감 표현은 한국 시간 기준의 날짜와 시각으로 적어 주세요.")
    hour, minute, second = next(iter(times), (23, 59, 0))
    day = next(iter(days))
    try:
        result = datetime(day.year, day.month, day.day, hour % 24, minute, second, tzinfo=SEOUL)
        return result + timedelta(days=1) if hour == 24 else result
    except (ValueError, OverflowError) as error:
        raise AiClientError("마감 시각이 지원 범위를 벗어났습니다.") from error
