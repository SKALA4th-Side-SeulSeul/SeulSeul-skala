"""공지에서 제목·요약·마감일을 구조화해 추출하고 검증한다."""

import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from seulseul.ai.client import MAX_RETRY_AFTER_SECONDS, AiClientError, ChatClient
from seulseul.ai.deadline import (
    expected_deadline,
    find_date_expressions,
    has_explicit_year,
    has_time_expression,
    parse_time_expressions,
    time_expression_count,
)
from seulseul.ai.model import NoticeAnalysis

MAX_NOTICE_INPUT_CHARS = 12_000
MAX_ANALYSIS_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2.0, 8.0)
SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")
REQUIRED_ANALYSIS_FIELDS = frozenset({"title", "summary", "deadline_at", "deadline_source_text"})
ISO_24_HOUR_PATTERN = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?P<separator>T| )24:00"
    r"(?P<seconds>:00(?:\.0+)?)?(?P<offset>Z|[+-]\d{2}:?\d{2})?$"
)
DEADLINE_AT_EXPECTED_FORMAT = "YYYY-MM-DDTHH:MM:SS+09:00"
MAX_DEADLINE_ERROR_CHARS = 255
MAX_DEADLINE_RECEIVED_CHARS = 80
DEADLINE_CONTEXT = re.compile(
    r"마감|기한|제출|신청|접수|등록|응답|참여|확인|완료|예약|deadline|due|submit|apply",
    re.IGNORECASE,
)
STRONG_DEADLINE_CONTEXT = re.compile(
    r"마감|기한|제출|신청|접수|등록|응답|참여|확인|완료|예약|deadline|due|submit|apply",
    re.IGNORECASE,
)
SCHEDULE_CONTEXT = re.compile(
    r"시간표|타임테이블|time\s*table|행사|진행|시작|종료|발표|부터",
    re.IGNORECASE,
)

ANALYSIS_SYSTEM_PROMPT = """\
너는 교육생용 Slack 공지를 구조화하는 분석기다.
공지 원문에 있는 정보만 사용하고 추측하거나 내용을 만들지 않는다.
대상 링크와 관련된 할 일을 기준으로 title과 summary를 작성한다.
신청 폼·제출 폼·등록 링크가 있으면 신청·제출·등록 마감을 deadline_at으로 선택한다.
진행 예정일·행사 일시는 deadline_at으로 선택하지 말고 summary에 보조 정보로 포함한다.
신청 마감과 행사 일시가 모두 있으면 신청 마감을 우선한다.
신청·제출 마감이 없을 때만 행사 진행 일시를 deadline_at으로 선택한다.
변경 전과 변경 후가 함께 있으면 변경 후의 일정을 선택한다.
deadline_source_text에는 날짜와 직접 연결된 시간 표현을 함께 인용한다.
날짜와 시간이 서로 다른 줄에 있더라도 각각 마감 문맥으로 직접 연결되어 있으면
두 원문 표현을 함께 인용한다.
"변경 후"나 "신청 마감" 같은 라벨을 날짜·시각 앞에 덧붙이지 않는다.
반드시 설명이나 Markdown 없이 다음 키를 가진 JSON 객체 하나만 반환한다.

{
  "title": "짧은 체크리스트 제목",
  "summary": "학생이 해야 할 행동을 포함한 요약",
  "deadline_at": "ISO 8601 마감 시각",
  "deadline_source_text": "원문에 실제로 있는 마감 표현"
}

시간대는 Asia/Seoul을 사용한다. `deadline_at`은 반드시 `+09:00` 오프셋을 포함한
ISO 8601 시각(예: `2026-09-30T18:00:00+09:00`)으로 반환한다. 시각이 없으면 23:59로 정한다.
`deadline_at`에는 `T24:00`을 사용하지 말고, 24:00·자정은 다음 날
`T00:00:00+09:00`으로 반환한다.
연도가 없으면 Slack 게시일의 연도를 사용하고, 상대 날짜는 Slack 게시 시각을 기준으로 계산한다.
"금일"은 "오늘"과 같으며 한국 시간 기준 Slack 최초 게시일의 당일이다.
"자정까지"는 해당 날짜의 24:00, 즉 다음 날 00:00으로 계산한다. 날짜 경계가
모호한 "자정"만 있는 경우에는 deadline_source_text에 "자정까지" 또는 "24:00"을 포함한다.
밤·저녁·새벽·아침, 오전·오후, 시 반, AM/PM, 초 단위, 명일·익일·내일모레·N일 뒤·월말 같은
원문 표현도 그대로 해석한다. 지원하지 않거나 의미가 모호한 날짜·시간은 23:59 또는 다른 시각으로
추측하지 말고 분석을 실패시킨다.
마감일은 정확히 하나여야 한다. deadline_source_text에는 선택한 마감 표현을 원문에서 인용한다.
마감 표현 자체에 시각이 있으면 날짜와 시각을 함께 인용하고, 날짜만 있는 마감이면 날짜만 인용한다.
공지에 포함된 시간표·행사 진행 시각은 마감 시각으로 사용하거나 deadline_source_text에 섞지 않는다.
title은 255자 이하여야 한다.

분석할 공지 본문은 사용자 메시지의 [공지 원문 시작]과 [공지 원문 끝] 사이에만 있다.
Slack 게시 시각, 대상 링크, 구분 표시는 메타데이터이므로 title·summary·deadline_source_text에
메타데이터 문구를 사용하지 않는다. deadline_source_text에는 선택한 마감일을 판단한
원문 근거를 사람이 이해할 수 있게 적는다. Slack 마크다운, 띄어쓰기, 숫자 표기(예:
18:00과 오후 6시), 주변 설명 문구는 달라도 되지만 원문에 없는 날짜·시각은 만들지 않는다.
"""


class NoticeAnalyzer:
    def __init__(
        self,
        client: ChatClient,
        *,
        max_input_chars: int = MAX_NOTICE_INPUT_CHARS,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_input_chars < 1:
            raise ValueError(f"max_input_chars는 1 이상이어야 합니다. 전달된 값: {max_input_chars}")
        self._client = client
        self._max_input_chars = max_input_chars
        self._sleeper = sleeper

    def analyze(self, notice_text: str, notice_url: str, posted_at: datetime) -> NoticeAnalysis:
        """공지 원문을 최대 3회 분석하고 검증된 결과를 반환한다."""
        if len(notice_text) > self._max_input_chars:
            raise AiClientError(
                f"공지 본문이 AI 입력 제한 {self._max_input_chars}자를 초과했습니다.",
                retryable=False,
            )
        if posted_at.tzinfo is None:
            raise ValueError("posted_at에는 시간대 정보가 있어야 합니다.")

        last_error: AiClientError | None = None
        for attempt in range(MAX_ANALYSIS_ATTEMPTS):
            if attempt > 0:
                default_delay = RETRY_DELAYS_SECONDS[attempt - 1]
                delay = (
                    last_error.retry_after_seconds
                    if last_error is not None and last_error.retry_after_seconds is not None
                    else default_delay
                )
                if not math.isfinite(delay) or not 0 <= delay <= MAX_RETRY_AFTER_SECONDS:
                    raise AiClientError(
                        "AI 재시도 대기시간이 유효하지 않거나 상한 60초를 초과했습니다.",
                        retryable=False,
                        retry_count=attempt - 1,
                    )
                self._sleeper(delay)

            feedback = ""
            if last_error is not None:
                feedback = "\n이전 응답은 검증에 실패했다. 형식을 바로잡아 다시 반환한다."
            user_prompt = (
                "[공지 원문 시작]\n"
                f"{notice_text}\n"
                "[공지 원문 끝]\n"
                "[분석용 메타데이터]\n"
                f"Slack 게시 시각: {posted_at.astimezone(SEOUL_TIMEZONE).isoformat()}\n"
                f"대상 링크: {notice_url}{feedback}"
            )
            try:
                raw_output = self._client.complete(ANALYSIS_SYSTEM_PROMPT, user_prompt)
                return parse_notice_analysis(raw_output, notice_text, posted_at)
            except AiClientError as error:
                error.retry_count = attempt
                last_error = error
                if not error.retryable:
                    raise

        assert last_error is not None
        raise last_error


def parse_notice_analysis(raw_output: str, notice_text: str, posted_at: datetime) -> NoticeAnalysis:
    """AI JSON을 제품 계약에 맞게 검증한다."""
    try:
        payload: Any = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise AiClientError("AI 응답이 JSON 객체가 아닙니다.") from error
    if not isinstance(payload, dict):
        raise AiClientError("AI 응답이 JSON 객체가 아닙니다.")

    missing_fields = REQUIRED_ANALYSIS_FIELDS - payload.keys()
    if missing_fields:
        raise AiClientError(f"AI 응답에 필수 필드가 없습니다: {', '.join(sorted(missing_fields))}")

    values: dict[str, str] = {}
    for field in REQUIRED_ANALYSIS_FIELDS:
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise AiClientError(f"AI 응답의 {field} 필드는 비어 있지 않은 문자열이어야 합니다.")
        values[field] = value.strip()
        if "\x00" in value:
            raise AiClientError(f"AI 응답의 {field}에 저장할 수 없는 문자가 있습니다.")
    if len(values["title"]) > 255:
        raise AiClientError("AI 제목이 DB 저장 한도 255자를 초과했습니다.")

    deadline_at_value = values["deadline_at"]
    try:
        deadline_at = _parse_iso_deadline(deadline_at_value)
    except ValueError as error:
        raise _deadline_at_error(
            "deadline_at.invalid_iso8601",
            deadline_at_value,
            f"{type(error).__name__}: {error}",
        ) from error
    if deadline_at.tzinfo is None:
        raise _deadline_at_error(
            "deadline_at.missing_timezone",
            deadline_at_value,
            "timezone offset is required",
        )
    try:
        deadline_at = deadline_at.astimezone(SEOUL_TIMEZONE)
    except (ValueError, OverflowError) as error:
        raise _deadline_at_error(
            "deadline_at.out_of_range",
            deadline_at_value,
            f"{type(error).__name__}: {error}",
        ) from error
    evidence = values["deadline_source_text"]
    # 날짜만 인용해 같은 줄의 명시 시각을 누락하는 경우도 검증한다.
    lines = [line for line in notice_text.splitlines() if evidence in line]
    if len(lines) == 1:
        evidence = lines[0]
    # 시간표처럼 마감과 무관한 시각 때문에 날짜만 있는 마감을 거부하지 않는다.
    # 다만 같은 문단의 "마감 시간"처럼 마감과 연결된 시각은 날짜와 함께 인용하게 한다.
    if _has_related_deadline_time(notice_text, evidence):
        raise AiClientError("마감 표현에 시각이 있으므로 날짜와 시각을 함께 인용해야 합니다.")
    evidence_without_urls = re.sub(r"https?://[^\s<>|]+", "", evidence)
    evidence_date_expressions = find_date_expressions(evidence_without_urls)
    if not evidence_date_expressions:
        raise AiClientError("AI 마감 근거에 날짜가 없습니다.")
    evidence_expected = _try_expected_deadline(evidence, posted_at)
    source_candidates = _source_deadline_candidates(notice_text, posted_at)
    if evidence_expected is not None and evidence_expected not in source_candidates:
        raise AiClientError("AI가 반환한 마감 날짜·시간이 공지 원문에서 확인되지 않습니다.")
    if not source_candidates:
        raise AiClientError("AI가 반환한 마감 날짜·시간이 공지 원문에서 확인되지 않습니다.")
    posted_at_seoul = posted_at.astimezone(SEOUL_TIMEZONE)
    if deadline_at not in source_candidates and not has_explicit_year(evidence_without_urls):
        repaired_candidates = [
            candidate
            for candidate in source_candidates
            if candidate >= posted_at_seoul and _same_month_day_and_time(deadline_at, candidate)
        ]
        if len(repaired_candidates) == 1:
            # 원문에 연도가 없으면 Slack 게시 연도를 사용한다는 제품 규칙을 적용한다.
            # 소형 모델이 날짜·시각은 맞추고 학습 데이터의 과거 연도만 붙이는 경우를 보정한다.
            deadline_at = repaired_candidates[0]
    if evidence_expected is not None and deadline_at != evidence_expected:
        raise AiClientError("AI가 반환한 deadline_at이 인용 근거와 일치하지 않습니다.")
    if deadline_at < posted_at_seoul:
        raise AiClientError("AI가 추출한 마감일이 Slack 게시 시각보다 과거입니다.")
    if deadline_at not in source_candidates:
        raise AiClientError("AI 마감일이 원문의 날짜·시각과 일치하지 않습니다.")

    return NoticeAnalysis(
        title=values["title"],
        summary=values["summary"],
        deadline_at=deadline_at,
        deadline_source_text=values["deadline_source_text"],
    )


def _parse_iso_deadline(value: str) -> datetime:
    """Python이 읽지 못하는 ISO 8601의 24:00을 다음 날 00:00으로 보정한다."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        match = ISO_24_HOUR_PATTERN.fullmatch(value)
        if match is None:
            raise
        normalized_value = (
            f"{match.group('date')}{match.group('separator')}00:00"
            f"{match.group('seconds') or ''}{match.group('offset') or ''}"
        )
        return datetime.fromisoformat(normalized_value) + timedelta(days=1)


def _deadline_at_error(code: str, received: str, reason: str) -> AiClientError:
    """마감 시각 오류를 DB·로그·대시보드에서 확인 가능한 형식으로 만든다."""
    prefix = (
        "AI 응답 형식 오류 | "
        f"code={code} | field=deadline_at | "
        f"expected_format={DEADLINE_AT_EXPECTED_FORMAT} | received="
    )
    received_preview = repr(_compact_diagnostic_value(received, MAX_DEADLINE_RECEIVED_CHARS))
    reason_prefix = " | reason="
    available_reason_chars = max(
        MAX_DEADLINE_ERROR_CHARS - len(prefix) - len(received_preview) - len(reason_prefix) - 2,
        0,
    )
    reason_preview = repr(_compact_diagnostic_value(reason, available_reason_chars))
    message = f"{prefix}{received_preview}{reason_prefix}{reason_preview}"
    return AiClientError(message[:MAX_DEADLINE_ERROR_CHARS])


def _compact_diagnostic_value(value: str, max_chars: int) -> str:
    """오류 기록에 포함할 값을 한 줄로 줄이고 저장 길이를 제한한다."""
    compact_value = " ".join(value.split())
    if len(compact_value) <= max_chars:
        return compact_value
    if max_chars <= 1:
        return compact_value[:max_chars]
    return f"{compact_value[: max_chars - 1]}…"


def _try_expected_deadline(evidence: str, posted_at: datetime) -> datetime | None:
    """날짜가 하나로 식별되는 AI 근거만 독립적으로 계산한다."""
    evidence_without_urls = re.sub(r"https?://[^\s<>|]+", "", evidence)
    date_expressions = find_date_expressions(evidence_without_urls)
    if len(date_expressions) != 1:
        return None
    try:
        return expected_deadline(evidence, posted_at)
    except AiClientError:
        return None


def _source_deadline_candidates(notice_text: str, posted_at: datetime) -> set[datetime]:
    """원문에서 독립적으로 계산 가능한 날짜·시각 후보를 반환한다."""
    source_without_urls = re.sub(r"https?://[^\s<>|]+", "", notice_text)
    lines = [line.strip() for line in source_without_urls.splitlines() if line.strip()]
    candidates: set[datetime] = set()
    strong_candidates: set[datetime] = set()
    has_strong_deadline_context = any(_is_strong_deadline_line(line) for line in lines)
    date_only_lines: list[str] = []
    time_only_lines: list[str] = []

    def add_candidate(snippet: str, *, strong: bool) -> None:
        try:
            candidate = expected_deadline(snippet, posted_at)
        except AiClientError:
            return
        candidates.add(candidate)
        if strong:
            strong_candidates.add(candidate)

    source_dates = find_date_expressions(source_without_urls)
    source_time_count = time_expression_count(source_without_urls)
    if len(source_dates) == 1 and source_time_count <= 1:
        add_candidate(source_without_urls, strong=_is_strong_deadline_line(source_without_urls))

    for line in lines:
        if _is_submission_start_line(line):
            continue
        line_dates = find_date_expressions(line)
        line_time_count = time_expression_count(line)
        if len(line_dates) == 1 and line_time_count <= 1:
            strong = _is_strong_deadline_line(line)
            add_candidate(line, strong=strong)
            if line_time_count == 0 and strong:
                date_only_lines.append(line)
        elif not line_dates and line_time_count == 1 and _is_strong_deadline_line(line):
            time_only_lines.append(line)

    # 날짜와 시간이 분리된 공지에서는 두 표현이 모두 마감 문맥일 때만 결합한다.
    # 행사·발표 시각을 날짜와 임의로 조합하지 않도록 강한 마감 라벨만 사용한다.
    if len(date_only_lines) == 1 and len(time_only_lines) == 1:
        add_candidate(f"{date_only_lines[0]} {time_only_lines[0]}", strong=True)
        try:
            date_only_candidate = expected_deadline(date_only_lines[0], posted_at)
        except AiClientError:
            date_only_candidate = None
        if date_only_candidate is not None:
            candidates.discard(date_only_candidate)
            strong_candidates.discard(date_only_candidate)

    if has_strong_deadline_context:
        return strong_candidates
    return candidates


def _has_related_deadline_time(notice_text: str, evidence: str) -> bool:
    """마감 근거에 누락된, 마감과 연결된 시각이 있는지 확인한다.

    공지에는 시간표·행사 일정처럼 마감과 무관한 시각이 자주 포함된다. 전체 원문에
    시각이 있다는 이유만으로 날짜 전용 마감을 거부하지 않고, 같은 문단의 마감 문맥
    또는 마감 시각 라인에서만 누락 시각을 검증한다.
    """
    if _contains_time_expression(evidence):
        return False

    source_without_urls = re.sub(r"https?://[^\s<>|]+", "", notice_text)
    if any(
        DEADLINE_CONTEXT.search(line)
        and _contains_time_expression(line)
        and not _looks_like_schedule_line(line)
        for line in source_without_urls.splitlines()
    ):
        return True

    paragraphs = re.split(r"\n\s*\n", source_without_urls)
    for paragraph in paragraphs:
        if evidence not in paragraph:
            continue
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        timed_lines = [line for line in lines if _contains_time_expression(line)]
        if not timed_lines:
            continue
        if any(DEADLINE_CONTEXT.search(line) for line in timed_lines):
            return True
        if DEADLINE_CONTEXT.search(paragraph) and any(
            not _looks_like_schedule_line(line) for line in timed_lines
        ):
            return True
    return False


def _contains_time_expression(text: str) -> bool:
    """한국어 시각 또는 명시적인 정오·자정 표현이 있는지 확인한다."""
    return has_time_expression(text)


def _looks_like_schedule_line(line: str) -> bool:
    """두 시각 범위나 시간표 문맥을 마감 시각 후보에서 제외한다."""
    if SCHEDULE_CONTEXT.search(line):
        return True
    try:
        return len(parse_time_expressions(line)) >= 2
    except AiClientError:
        # 미해석 시각은 일정 범위가 아니라 검증 실패 사유다. 후보에서 제외하되
        # 강한 마감 문맥이 있다는 사실은 유지해 행사 시각으로 대체하지 않는다.
        return False


def _is_strong_deadline_line(line: str) -> bool:
    """제출·마감 문맥을 행사 시작 시각과 구분한다."""
    return (
        bool(STRONG_DEADLINE_CONTEXT.search(line))
        and not _is_submission_start_line(line)
        and not _looks_like_schedule_line(line)
    )


def _is_submission_start_line(line: str) -> bool:
    """제출·신청의 시작 시각을 마감 후보로 저장하지 않는다."""
    return bool(DEADLINE_CONTEXT.search(line) and re.search(r"시작|개시|부터", line))


def _same_month_day_and_time(first: datetime, second: datetime) -> bool:
    """연도만 다른 두 Asia/Seoul 마감 시각이 같은지 확인한다."""
    return (
        first.month,
        first.day,
        first.hour,
        first.minute,
        first.second,
        first.microsecond,
    ) == (
        second.month,
        second.day,
        second.hour,
        second.minute,
        second.second,
        second.microsecond,
    )
