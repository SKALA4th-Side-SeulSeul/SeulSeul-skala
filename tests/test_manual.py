"""운영자 수동 공지 CLI의 입력 검증과 이벤트 변환을 확인한다."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.manual import (
    build_manual_event,
    parse_manual_analysis,
    parse_slack_permalink,
)
from seulseul.notices.repository import InMemoryNoticeRepository
from seulseul.notices.service import NoticeService

SEOUL = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://workspace.slack.com/archives/C123ABC4567/p1789559318987269"
MESSAGE_TS = "1789559318.987269"


def test_parse_slack_permalink_extracts_channel_and_message_ts() -> None:
    assert parse_slack_permalink(SOURCE_URL) == ("C123ABC4567", MESSAGE_TS)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/archives/C123ABC4567/p1789559318987269",
        "https://workspace.slack.com/archives/C123ABC4567/not-a-message",
        "https://workspace.slack.com/archives/U123ABC4567/p1789559318987269",
    ],
)
def test_parse_slack_permalink_rejects_non_slack_message_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Slack 원문 링크"):
        parse_slack_permalink(url)


def test_build_manual_event_uses_created_or_changed_identity() -> None:
    created = build_manual_event("C123ABC4567", MESSAGE_TS, "공지", kind="created")
    changed = build_manual_event("C123ABC4567", MESSAGE_TS, "수정", kind="changed")

    assert created["ts"] == MESSAGE_TS
    assert changed["message"]["ts"] == MESSAGE_TS
    assert changed["message"]["edited"]["ts"] > MESSAGE_TS


def test_parse_manual_analysis_assumes_seoul_when_offset_is_omitted() -> None:
    analysis = parse_manual_analysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline="2026-09-30 23:59",
        deadline_source_text="9월 30일 23:59",
    )

    assert analysis == NoticeAnalysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline_at=datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL),
        deadline_source_text="9월 30일 23:59",
    )


def test_manual_events_register_update_and_delete_same_source() -> None:
    service = NoticeService(
        {"C123ABC4567"},
        repository=InMemoryNoticeRepository(10),
    )
    analysis = parse_manual_analysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline="2026-09-30 23:59",
        deadline_source_text="9월 30일 23:59",
    )
    created = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, "https://forms.example/task", kind="created"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        manual_analysis=analysis,
    )
    changed = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, "https://forms.example/task", kind="changed"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        manual_analysis=parse_manual_analysis(
            title="수정 설문",
            summary="수정된 설문을 제출합니다.",
            deadline="2026-10-01 23:59",
            deadline_source_text="10월 1일 23:59",
        ),
    )
    deleted = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, kind="deleted"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
    )

    assert created[0].analysis == analysis
    assert changed[0].analysis is not None and changed[0].analysis.title == "수정 설문"
    assert deleted[0].deleted_at is not None
