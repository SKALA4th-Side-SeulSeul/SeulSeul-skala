"""설정 채널의 Slack 메시지에서 링크별 공지를 수집한다."""

import logging
import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from seulseul.ai.client import AiClientError
from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.events import NoticeMessageEvent, parse_notice_event
from seulseul.notices.model import Notice
from seulseul.notices.repository import (
    AmbiguousNoticeError,
    InMemoryNoticeRepository,
    NoticeRepository,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_STORED_NOTICES = 50
NOTICE_URL_KEYWORDS = ("form", "docs")
URL_PATTERN = re.compile(r"https?://[^\s<>|]+", re.IGNORECASE)
URL_TRAILING_PUNCTUATION = ".,;:!?)]}"
SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")


class NoticeAnalyzerProtocol(Protocol):
    def analyze(self, notice_text: str, notice_url: str, posted_at: datetime) -> NoticeAnalysis: ...


class NoticeRetryError(Exception):
    """선택한 공지를 재처리할 수 없을 때 발생한다."""


def extract_notice_urls(text: str) -> tuple[tuple[str, str], ...]:
    """원문에서 처리 대상 URL과 canonical URL 쌍을 원래 순서대로 반환한다."""
    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in URL_PATTERN.finditer(text):
        original_url = match.group(0).rstrip(URL_TRAILING_PUNCTUATION)
        try:
            parsed = urlsplit(original_url)
            if not parsed.hostname:
                continue
        except ValueError:
            continue
        searchable_part = f"{parsed.hostname or ''}{parsed.path}".lower()
        if not any(keyword in searchable_part for keyword in NOTICE_URL_KEYWORDS):
            continue
        canonical_url = canonicalize_url(original_url)
        if canonical_url in seen:
            continue
        seen.add(canonical_url)
        links.append((original_url, canonical_url))
    return tuple(links)


def canonicalize_url(url: str) -> str:
    """중복 판정을 위해 scheme·host를 소문자로 만들고 query와 fragment를 제거한다."""
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


class NoticeService:
    def __init__(
        self,
        allowed_channel_ids: Collection[str],
        max_stored_notices: int = DEFAULT_MAX_STORED_NOTICES,
        analyzer: NoticeAnalyzerProtocol | None = None,
        repository: NoticeRepository | None = None,
        on_change: Callable[[], None] = lambda: None,
    ) -> None:
        if not allowed_channel_ids:
            raise ValueError("공지 채널 ID를 하나 이상 설정해야 합니다.")
        if max_stored_notices < 1:
            raise ValueError(
                f"max_stored_notices는 1 이상이어야 합니다. 전달된 값: {max_stored_notices}"
            )
        self._allowed_channel_ids = frozenset(allowed_channel_ids)
        self._analyzer = analyzer
        self._repository = repository or InMemoryNoticeRepository(max_stored_notices)
        self._on_change = on_change

    def parse_event(
        self, event: Mapping[str, Any], bot_user_id: str | None, bot_id: str | None = None
    ) -> NoticeMessageEvent | None:
        parsed = parse_notice_event(event, self._allowed_channel_ids, bot_user_id, bot_id)
        if parsed is not None and parsed.kind == "created" and not extract_notice_urls(parsed.text):
            return None
        return parsed

    def stored_permalink(self, workspace_id: str, event: NoticeMessageEvent) -> str | None:
        notices = self._repository.source_notices(workspace_id, event.channel_id, event.message_ts)
        return notices[0].source_permalink if notices else None

    @staticmethod
    def needs_permalink(event: NoticeMessageEvent) -> bool:
        return event.kind != "deleted" and bool(extract_notice_urls(event.text))

    def record_channel_message(
        self,
        event: Mapping[str, Any],
        *,
        workspace_id: str,
        source_permalink: str = "",
        bot_user_id: str | None = None,
        bot_id: str | None = None,
    ) -> list[Notice]:
        """최신 원본 이벤트만 적용하고 링크별 공지·체크리스트를 함께 동기화한다."""
        parsed = self.parse_event(event, bot_user_id, bot_id)
        if parsed is None:
            return []
        if not workspace_id or (self.needs_permalink(parsed) and not source_permalink):
            raise ValueError("workspace_id와 source_permalink는 비어 있을 수 없습니다.")
        if not self._repository.begin_event(workspace_id, parsed):
            return []
        originals = {
            notice.canonical_url: notice
            for notice in self._repository.source_notices(
                workspace_id, parsed.channel_id, parsed.message_ts
            )
        }
        posted_at = datetime.fromtimestamp(float(parsed.message_ts), timezone.utc).astimezone(
            SEOUL_TIMEZONE
        )
        results = []
        for original_url, canonical_url in extract_notice_urls(parsed.text):
            original = originals.get(canonical_url)
            if original is not None and (
                parsed.kind == "created"
                or (
                    original.deleted_at is None
                    and original.text == parsed.text
                    and original.original_url == original_url
                )
            ):
                results.append(original)
                continue
            analyzed = self._build_notice(
                workspace_id=workspace_id,
                channel_id=parsed.channel_id,
                message_ts=parsed.message_ts,
                text=parsed.text,
                original_url=original_url,
                canonical_url=canonical_url,
                source_permalink=source_permalink,
                posted_at=posted_at,
            )
            if (
                analyzed.processing_status != "processed"
                and original is not None
                and original.deleted_at is None
                and original.analysis is not None
            ):
                # 유지 링크는 마지막 성공 분석을 보존한다. 제거 후 재추가한 링크는
                # 새 분석이 성공하기 전까지 예전 분석으로 복구하지 않는다.
                analyzed = replace(
                    analyzed,
                    analysis=original.analysis,
                    processing_status="processing_failed",
                    last_error=analyzed.last_error
                    or "AI_PROVIDER가 비어 있어 수정 내용을 분석하지 못했습니다.",
                )
            results.append(analyzed)
        changed = self._repository.apply_event(workspace_id, parsed, results)
        if changed and any(n.processing_status == "processed" or n.deleted_at for n in changed):
            self._on_change()
        return changed or []

    def recent_notices(self, limit: int, *, workspace_id: str | None = None) -> list[Notice]:
        if limit < 1:
            raise ValueError(f"limit은 1 이상이어야 합니다. 전달된 값: {limit}")
        return self._repository.recent(limit, workspace_id)

    def failed_notices(self, limit: int, *, workspace_id: str | None = None) -> list[Notice]:
        if limit < 1:
            raise ValueError(f"limit은 1 이상이어야 합니다. 전달된 값: {limit}")
        return self._repository.failed(limit, self._allowed_channel_ids, workspace_id)

    def retry_failed_notice(
        self,
        workspace_id: str,
        notice_url: str,
        channel_id: str | None = None,
        message_ts: str | None = None,
    ) -> Notice:
        """저장된 원문으로 실패 공지 하나를 다시 분석해 같은 기록을 갱신한다."""
        if self._analyzer is None:
            raise NoticeRetryError("AI_PROVIDER가 비어 있습니다. AI 설정 후 다시 실행해 주세요.")
        try:
            parsed = urlsplit(notice_url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Invalid notice URL")
            canonical_url = canonicalize_url(notice_url)
        except ValueError as error:
            raise NoticeRetryError("공지의 올바른 http/https 제출 링크를 입력해 주세요.") from error
        try:
            original = self._repository.get(workspace_id, canonical_url, channel_id, message_ts)
        except AmbiguousNoticeError as error:
            raise NoticeRetryError(str(error)) from error
        if original is None or original.deleted_at is not None:
            raise NoticeRetryError("해당 워크스페이스에서 재처리할 공지를 찾을 수 없습니다.")
        if original.channel_id not in self._allowed_channel_ids:
            raise NoticeRetryError("현재 설정된 공지 채널의 공지만 재처리할 수 있습니다.")
        if original.processing_status != "processing_failed":
            raise NoticeRetryError("AI 분석에 실패한 공지만 재처리할 수 있습니다.")

        analyzed = self._build_notice(
            workspace_id=original.workspace_id,
            channel_id=original.channel_id,
            message_ts=original.message_ts,
            text=original.text,
            original_url=original.original_url,
            canonical_url=original.canonical_url,
            source_permalink=original.source_permalink,
            posted_at=original.posted_at,
        )
        # 수정 분석 실패로 남아 있던 기존 요약이 있다면 실패 시에도 유지한다(D-011).
        if analyzed.processing_status == "processing_failed":
            analyzed = replace(analyzed, analysis=original.analysis)
        if not self._repository.replace_failed(original, analyzed):
            raise NoticeRetryError("분석 중 공지 상태가 변경되었습니다. 목록을 다시 확인해 주세요.")
        if analyzed.processing_status == "processed":
            self._on_change()
        return analyzed

    def _build_notice(
        self,
        *,
        workspace_id: str,
        channel_id: str,
        message_ts: str,
        text: str,
        original_url: str,
        canonical_url: str,
        source_permalink: str,
        posted_at: datetime,
    ) -> Notice:
        if self._analyzer is None:
            return Notice(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_ts=message_ts,
                text=text,
                original_url=original_url,
                canonical_url=canonical_url,
                source_permalink=source_permalink,
                posted_at=posted_at,
                processing_status="ai_disabled",
            )
        try:
            analysis = self._analyzer.analyze(text, original_url, posted_at)
        except AiClientError as error:
            logger.warning(
                "공지 AI 분석 실패: channel=%s ts=%s url=%s 원인=%s",
                channel_id,
                message_ts,
                canonical_url,
                error,
            )
            return Notice(
                workspace_id=workspace_id,
                channel_id=channel_id,
                message_ts=message_ts,
                text=text,
                original_url=original_url,
                canonical_url=canonical_url,
                source_permalink=source_permalink,
                posted_at=posted_at,
                processing_status="processing_failed",
                retry_count=error.retry_count,
                last_error=str(error),
            )
        return Notice(
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_ts=message_ts,
            text=text,
            original_url=original_url,
            canonical_url=canonical_url,
            source_permalink=source_permalink,
            posted_at=posted_at,
            processing_status="processed",
            analysis=analysis,
        )
