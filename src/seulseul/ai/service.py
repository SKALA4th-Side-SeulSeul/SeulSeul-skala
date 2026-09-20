"""공지에서 제목·요약·마감일을 구조화해 추출하고 검증한다."""

import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from seulseul.ai.client import MAX_RETRY_AFTER_SECONDS, AiClientError, ChatClient
from seulseul.ai.deadline import CLOCK, expected_deadline
from seulseul.ai.model import NoticeAnalysis

MAX_NOTICE_INPUT_CHARS = 12_000
MAX_ANALYSIS_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (2.0, 8.0)
SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")
REQUIRED_ANALYSIS_FIELDS = frozenset({"title", "summary", "deadline_at", "deadline_source_text"})

ANALYSIS_SYSTEM_PROMPT = """\
너는 교육생용 Slack 공지를 구조화하는 분석기다.
공지 원문에 있는 정보만 사용하고 추측하거나 내용을 만들지 않는다.
대상 링크와 관련된 할 일을 기준으로 title과 summary를 작성한다.
반드시 설명이나 Markdown 없이 다음 키를 가진 JSON 객체 하나만 반환한다.

{
  "title": "짧은 체크리스트 제목",
  "summary": "학생이 해야 할 행동을 포함한 요약",
  "deadline_at": "ISO 8601 마감 시각",
  "deadline_source_text": "원문에 실제로 있는 마감 표현"
}

시간대는 Asia/Seoul을 사용한다. 시각이 없으면 23:59로 정한다.
연도가 없으면 Slack 게시일의 연도를 사용하고, 상대 날짜는 Slack 게시 시각을 기준으로 계산한다.
마감일은 정확히 하나여야 한다. deadline_source_text에는 날짜와 시각을 함께 인용한다.
title은 255자 이하여야 한다."""


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
                f"Slack 게시 시각: {posted_at.astimezone(SEOUL_TIMEZONE).isoformat()}\n"
                f"대상 링크: {notice_url}\n"
                f"공지 원문:\n{notice_text}{feedback}"
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

    try:
        deadline_at = datetime.fromisoformat(values["deadline_at"])
    except ValueError as error:
        raise AiClientError(
            "AI 응답의 deadline_at을 ISO 8601 시각으로 해석할 수 없습니다."
        ) from error
    if deadline_at.tzinfo is None:
        raise AiClientError("AI 응답의 deadline_at에 시간대가 없습니다.")
    try:
        deadline_at = deadline_at.astimezone(SEOUL_TIMEZONE)
    except (ValueError, OverflowError) as error:
        raise AiClientError("AI 마감일이 지원 범위를 벗어났습니다.") from error
    if deadline_at < posted_at.astimezone(SEOUL_TIMEZONE):
        raise AiClientError("AI가 추출한 마감일이 Slack 게시 시각보다 과거입니다.")
    if values["deadline_source_text"] not in notice_text:
        raise AiClientError("AI가 반환한 마감 표현이 공지 원문에 없습니다.")
    evidence = values["deadline_source_text"]
    # 날짜만 인용해 같은 줄의 명시 시각을 누락하는 경우도 검증한다.
    lines = [line for line in notice_text.splitlines() if evidence in line]
    if len(lines) == 1:
        evidence = lines[0]
    # 다른 줄에 있는 시각을 날짜만 인용하여 23:59로 덮어쓰지 못하게 한다.
    # 관련 시각인지 확신할 수 없으면 AI가 날짜·시각을 포함해 다시 인용하게 한다.
    source_without_urls = re.sub(r"https?://[^\s<>|]+", "", notice_text)
    if (
        not CLOCK.search(evidence)
        and (
            CLOCK.search(source_without_urls)
            or any(word in source_without_urls for word in ("정오", "자정", "오전", "오후"))
        )
        and not any(word in evidence for word in ("정오", "자정"))
    ):
        raise AiClientError(
            "원문에 시각이 있습니다. 마감 근거에 날짜와 시각을 함께 인용해야 합니다."
        )
    if deadline_at != expected_deadline(evidence, posted_at):
        raise AiClientError("AI 마감일이 원문의 날짜·시각과 일치하지 않습니다.")

    return NoticeAnalysis(
        title=values["title"],
        summary=values["summary"],
        deadline_at=deadline_at,
        deadline_source_text=values["deadline_source_text"],
    )
