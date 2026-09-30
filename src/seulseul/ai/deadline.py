"""AI가 인용한 마감 표현을 독립적으로 계산한다. 불명확하면 추측하지 않는다."""

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from seulseul.ai.client import AiClientError

SEOUL = ZoneInfo("Asia/Seoul")
DATE = re.compile(
    r"(?<!\d)(?:(?P<year>\d{4})\s*(?:년\s*|[-/.]))?"
    r"(?P<month>\d{1,2})\s*(?:월\s*|[-/.])(?P<day>\d{1,2})(?:\s*일)?(?!\d)"
)
RELATIVE_MONTH_DATE = re.compile(r"(?P<scope>이번|다음)\s*달\s*(?P<day>\d{1,2})\s*일?")
MONTH_END_DATE = re.compile(
    r"(?:(?:(?P<year>\d{4})\s*년\s*)?(?P<month>\d{1,2})\s*월\s*"
    r"(?:말일|말|마지막\s*날)|(?P<scope>이번|다음)\s*달\s*"
    r"(?:말일|말|마지막\s*날)|월\s*말(?:일)?)"
)
RELATIVE = re.compile(
    r"내일\s*모레|"
    r"(?:하루|이틀|사흘|나흘|닷새|엿새|이레|여드레|아흐레|열흘|"
    r"\d+\s*일)\s*(?:뒤|후)|"
    r"(?:(?:이번|다음)\s*주|금주|차주)\s*[월화수목금토일]요일|"
    r"오늘|금일|당일|내일|명일|익일|모레|글피|다음\s*날"
)
RELATIVE_DAY_OFFSETS = {
    "오늘": 0,
    "금일": 0,
    "당일": 0,
    "내일": 1,
    "명일": 1,
    "익일": 1,
    "다음날": 1,
    "모레": 2,
    "내일모레": 2,
    "글피": 3,
}
RELATIVE_KOREAN_DAY_OFFSETS = {
    "하루": 1,
    "이틀": 2,
    "사흘": 3,
    "나흘": 4,
    "닷새": 5,
    "엿새": 6,
    "이레": 7,
    "여드레": 8,
    "아흐레": 9,
    "열흘": 10,
}
KOREAN_HOURS = {
    "한": 1,
    "두": 2,
    "세": 3,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
    "열한": 11,
    "열두": 12,
}
_HOUR_TOKEN = r"(?:\d{1,2}|" + "|".join(KOREAN_HOURS) + r")"
_KOREAN_PERIOD = r"오전|오후|새벽|아침|저녁|밤"
_LATIN_PERIOD = r"a\.?m\.?|p\.?m\.?"

KOREAN_CLOCK = re.compile(
    rf"(?<![\d가-힣])(?:(?P<period>{_KOREAN_PERIOD})\s*)?"
    rf"(?P<hour>{_HOUR_TOKEN})\s*시"
    rf"(?:\s*반|\s*(?P<minute>\d{{1,2}})\s*분"
    rf"(?:\s*(?P<second>\d{{1,2}})\s*초)?)?"
    r"(?!작|간|점|행)",
    re.IGNORECASE,
)
NUMERIC_COLON_CLOCK = re.compile(
    rf"(?<![\d가-힣])(?:(?P<period>{_KOREAN_PERIOD})\s*)?"
    r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{1,2})"
    r"(?:\s*:\s*(?P<second>\d{1,2}))?(?!\d)",
    re.IGNORECASE,
)
AMPM_CLOCK = re.compile(
    rf"(?<![\d가-힣])(?P<hour>\d{{1,2}})"
    r"(?:\s*:\s*(?P<minute>\d{1,2})(?:\s*:\s*(?P<second>\d{1,2}))?)?"
    rf"\s*(?P<period>{_LATIN_PERIOD})(?![A-Za-z])",
    re.IGNORECASE,
)
# 호환성을 위해 남긴 숫자형 시각 정규식이다. 실제 검증은 parse_time_expressions가 담당한다.
CLOCK = re.compile(
    rf"(?<![\d가-힣])(?:(?:{_KOREAN_PERIOD})\s*)?\d{{1,2}}"
    r"(?:\s*:\s*\d{1,2}(?:\s*:\s*\d{1,2})?|\s*시(?:\s*\d{1,2}\s*분)?"
    r"(?:\s*\d{1,2}\s*초)?)",
    re.IGNORECASE,
)
SYMBOLIC_CLOCK = re.compile(r"정오|자정")
TIME_HINT = re.compile(
    rf"(?:{_KOREAN_PERIOD}|정오|자정|"
    rf"(?<!\d)\d{{1,2}}\s*(?:{_LATIN_PERIOD})|"
    r"(?<!\d)\d{1,2}\s*:\s*\d{1,2}(?:\s*:\s*\d{1,2})?|"
    rf"(?<!\d){_HOUR_TOKEN}\s*시|\d{{1,3}}\s*분|\d{{1,3}}\s*초)",
    re.IGNORECASE,
)
END_OF_DAY_MIDNIGHT = re.compile(
    r"자정\s*까지|"
    r"(?:마감|기한|마감일|제출\s*(?:기한|마감)|신청\s*(?:기한|마감)|"
    r"접수\s*(?:기한|마감)|등록\s*(?:기한|마감)|응답\s*(?:기한|마감))"
    r"[^\n.!?]{0,30}자정(?!\s*(?:부터|시작))"
)


@dataclass(frozen=True)
class ParsedTime:
    """원문에서 찾은 시각과 날짜 경계 이동 정보를 보관한다."""

    start: int
    end: int
    hour: int
    minute: int
    second: int
    day_offset: int = 0


def find_date_expressions(text: str) -> list[re.Match[str]]:
    """지원하는 날짜 표현을 겹치지 않게 찾아 원문 순서로 반환한다."""
    matches: list[re.Match[str]] = []
    for pattern in (DATE, RELATIVE_MONTH_DATE, MONTH_END_DATE, RELATIVE):
        matches.extend(pattern.finditer(text))
    matches.sort(key=lambda match: (match.start(), -(match.end() - match.start())))

    selected: list[re.Match[str]] = []
    for match in matches:
        if any(
            match.start() < selected_match.end() and selected_match.start() < match.end()
            for selected_match in selected
        ):
            continue
        selected.append(match)
    return sorted(selected, key=lambda match: match.start())


def has_explicit_year(text: str) -> bool:
    """날짜 표현 중 네 자리 연도가 명시되어 있는지 확인한다."""
    return any(
        match.re in {DATE, MONTH_END_DATE} and bool(match.groupdict().get("year"))
        for match in find_date_expressions(text)
    )


def parse_time_expressions(text: str) -> list[ParsedTime]:
    """지원 가능한 시각을 찾고, 시각처럼 보이지만 해석하지 못한 표현은 거절한다."""
    candidates: list[ParsedTime] = []
    for match in KOREAN_CLOCK.finditer(text):
        candidates.append(_parsed_korean_clock(match))
    for match in NUMERIC_COLON_CLOCK.finditer(text):
        candidates.append(_parsed_numeric_clock(match))
    for match in AMPM_CLOCK.finditer(text):
        candidates.append(_parsed_numeric_clock(match))
    for match in SYMBOLIC_CLOCK.finditer(text):
        candidates.append(
            ParsedTime(
                start=match.start(),
                end=match.end(),
                hour=12 if match[0] == "정오" else 0,
                minute=0,
                second=0,
                day_offset=0 if match[0] == "정오" else 1,
            )
        )

    selected = _select_non_overlapping_times(candidates)
    for hint in TIME_HINT.finditer(text):
        if not any(
            parsed.start <= hint.start() and hint.end() <= parsed.end for parsed in selected
        ):
            raise AiClientError(
                "원문의 마감 시각을 해석할 수 없습니다. 지원 형식으로 명시해 주세요."
            )
    return selected


def time_expression_count(text: str) -> int:
    """원문에 있는 시각 수를 반환하며, 미해석 시각은 후보에서 제외할 수 있게 표시한다."""
    try:
        return len(parse_time_expressions(text))
    except AiClientError:
        return 2


def has_time_expression(text: str) -> bool:
    """지원 여부와 관계없이 원문에 시각 표현이 있는지 확인한다."""
    try:
        return bool(parse_time_expressions(text))
    except AiClientError:
        return bool(TIME_HINT.search(text))


def expected_deadline(evidence: str, posted_at: datetime) -> datetime:
    """지원하는 날짜·상대 날짜·시각 표현을 한국 시간 기준으로 계산한다."""
    posted = posted_at.astimezone(SEOUL)
    days: set[date] = set()
    try:
        for match in find_date_expressions(evidence):
            days.add(_resolve_date_match(match, posted.date()))
    except (ValueError, OverflowError) as error:
        raise AiClientError("원문의 마감 날짜가 유효하지 않습니다.") from error
    if len(days) != 1:
        raise AiClientError(
            "마감 표현에서 날짜 하나를 확정할 수 없습니다. 명시적인 날짜가 필요합니다."
        )

    if "자정" in evidence and not END_OF_DAY_MIDNIGHT.search(evidence):
        # 시작 시각이나 날짜 경계가 불명확한 '자정'은 마감 시각으로 추측하지 않는다.
        raise AiClientError("자정은 날짜 경계가 모호합니다. 자정까지 또는 24:00으로 적어 주세요.")

    if re.search(r"UTC|GMT|[+-]\d{2}:\d{2}", evidence, re.IGNORECASE):
        raise AiClientError("마감 표현은 한국 시간 기준의 날짜와 시각으로 적어 주세요.")

    times = parse_time_expressions(evidence)
    if len(times) > 1:
        raise AiClientError("마감 표현에 시각이 여러 개 있습니다.")
    day = next(iter(days))
    if not times:
        hour, minute, second, day_offset = 23, 59, 0, 0
    else:
        parsed = times[0]
        hour, minute, second, day_offset = (
            parsed.hour,
            parsed.minute,
            parsed.second,
            parsed.day_offset,
        )
    try:
        result_day = day + timedelta(days=day_offset)
        return datetime(
            result_day.year,
            result_day.month,
            result_day.day,
            hour,
            minute,
            second,
            tzinfo=SEOUL,
        )
    except (ValueError, OverflowError) as error:
        raise AiClientError("마감 시각이 지원 범위를 벗어났습니다.") from error


def _resolve_date_match(match: re.Match[str], posted_day: date) -> date:
    if match.re is DATE:
        year = int(match.group("year") or posted_day.year)
        return date(year, int(match.group("month")), int(match.group("day")))

    if match.re is RELATIVE_MONTH_DATE:
        scope = match.group("scope")
        year, month = _shift_month(posted_day.year, posted_day.month, 1 if scope == "다음" else 0)
        return date(year, month, int(match.group("day")))

    if match.re is MONTH_END_DATE:
        text = re.sub(r"\s+", "", match[0])
        if match.groupdict().get("month"):
            year = int(match.group("year") or posted_day.year)
            month = int(match.group("month"))
        elif text.startswith("다음달"):
            year, month = _shift_month(posted_day.year, posted_day.month, 1)
        else:
            year, month = posted_day.year, posted_day.month
        return date(year, month, calendar.monthrange(year, month)[1])

    phrase = re.sub(r"\s+", "", match[0])
    if phrase in RELATIVE_DAY_OFFSETS:
        return posted_day + timedelta(days=RELATIVE_DAY_OFFSETS[phrase])
    if phrase in RELATIVE_KOREAN_DAY_OFFSETS:
        return posted_day + timedelta(days=RELATIVE_KOREAN_DAY_OFFSETS[phrase])
    numeric_days = re.fullmatch(r"(\d+)일(?:뒤|후)", phrase)
    if numeric_days:
        return posted_day + timedelta(days=int(numeric_days[1]))
    korean_days = re.fullmatch(r"(.+?)(?:뒤|후)", phrase)
    if korean_days and korean_days[1] in RELATIVE_KOREAN_DAY_OFFSETS:
        return posted_day + timedelta(days=RELATIVE_KOREAN_DAY_OFFSETS[korean_days[1]])

    week_match = re.fullmatch(r"(?:(이번|다음)주|(금주|차주))([월화수목금토일])요일", phrase)
    if week_match:
        target_weekday = "월화수목금토일".index(week_match[3])
        offset = target_weekday - posted_day.weekday()
        scope = week_match[1] or week_match[2]
        if scope in {"다음", "차주"}:
            offset += 7
        return posted_day + timedelta(days=offset)
    raise AiClientError("원문의 상대 날짜를 해석할 수 없습니다.")


def _shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    shifted = year * 12 + month - 1 + offset
    return shifted // 12, shifted % 12 + 1


def _parsed_korean_clock(match: re.Match[str]) -> ParsedTime:
    hour_token = match["hour"]
    hour = int(hour_token) if hour_token.isdigit() else KOREAN_HOURS[hour_token]
    minute = 30 if "반" in match[0] else int(match["minute"] or 0)
    second = int(match["second"] or 0)
    return _normalize_clock(match.start(), match.end(), hour, minute, second, match["period"])


def _parsed_numeric_clock(match: re.Match[str]) -> ParsedTime:
    return _normalize_clock(
        match.start(),
        match.end(),
        int(match["hour"]),
        int(match["minute"] or 0),
        int(match["second"] or 0),
        match["period"],
    )


def _normalize_clock(
    start: int,
    end: int,
    hour: int,
    minute: int,
    second: int,
    period: str | None,
) -> ParsedTime:
    if minute > 59 or second > 59:
        raise AiClientError("원문의 마감 시각이 유효하지 않습니다.")
    day_offset = 0
    normalized_period = period.lower().replace(".", "") if period else None
    if normalized_period:
        if not 1 <= hour <= 12:
            raise AiClientError("원문의 오전/오후 마감 시각이 유효하지 않습니다.")
        if normalized_period in {"오후", "저녁", "밤", "pm"}:
            hour = hour % 12 + 12
            if normalized_period in {"저녁", "밤"} and hour == 12:
                hour = 0
                day_offset = 1
        else:
            hour %= 12
    elif hour > 24:
        raise AiClientError("원문의 마감 시각이 유효하지 않습니다.")
    if hour == 24:
        if minute or second:
            raise AiClientError("24시는 분·초 없이 사용해야 합니다.")
        hour = 0
        day_offset = 1
    return ParsedTime(start, end, hour, minute, second, day_offset)


def _select_non_overlapping_times(candidates: list[ParsedTime]) -> list[ParsedTime]:
    selected: list[ParsedTime] = []
    for candidate in sorted(candidates, key=lambda item: (item.start, -(item.end - item.start))):
        if any(
            candidate.start < existing.end and existing.start < candidate.end
            for existing in selected
        ):
            continue
        selected.append(candidate)
    return sorted(selected, key=lambda item: item.start)
