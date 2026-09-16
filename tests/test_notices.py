"""설정 채널의 메시지에서 링크별 공지를 안전하게 수집하는지 확인한다."""

import logging
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint

from seulseul.ai.client import AiClientError
from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.model import Notice, NoticeModel
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import (
    NoticeService,
    canonicalize_url,
    extract_notice_urls,
    is_new_notice_message,
)

ALLOWED_CHANNEL = "C0000000001"
WORKSPACE_ID = "T0000000001"
MESSAGE_TS = "1789344000.000100"
PERMALINK = "https://workspace.slack.com/archives/C0000000001/p1789344000000100"
SEOUL = ZoneInfo("Asia/Seoul")


def channel_message(**overrides: Any) -> dict[str, Any]:
    message = {
        "type": "message",
        "channel": ALLOWED_CHANNEL,
        "channel_type": "channel",
        "ts": MESSAGE_TS,
        "text": "9월 20일까지 제출 https://forms.example.test/task",
        "user": "U0000000001",
    }
    message.update(overrides)
    return message


class FakeAnalyzer:
    def __init__(self, error: AiClientError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, datetime]] = []

    def analyze(self, notice_text: str, notice_url: str, posted_at: datetime) -> NoticeAnalysis:
        self.calls.append((notice_text, notice_url, posted_at))
        if self.error is not None:
            raise self.error
        return NoticeAnalysis(
            title="과제 제출",
            summary="과제를 제출합니다.",
            deadline_at=datetime(2026, 9, 20, 23, 59, tzinfo=SEOUL),
            deadline_source_text="9월 20일까지",
        )


class FakeNoticeRepository:
    def __init__(self) -> None:
        self.notices: list[Notice] = []

    def contains(self, workspace_id: str, canonical_url: str) -> bool:
        return any(
            notice.workspace_id == workspace_id and notice.canonical_url == canonical_url
            for notice in self.notices
        )

    def add(self, notice: Notice) -> bool:
        if self.contains(notice.workspace_id, notice.canonical_url):
            return False
        self.notices.append(notice)
        return True

    def recent(self, limit: int, workspace_id: str | None = None) -> list[Notice]:
        notices = reversed(self.notices)
        if workspace_id is not None:
            return [notice for notice in notices if notice.workspace_id == workspace_id][:limit]
        return list(notices)[:limit]


def record(service: NoticeService, event: dict[str, Any] | None = None) -> list[Notice]:
    return service.record_channel_message(
        event or channel_message(),
        workspace_id=WORKSPACE_ID,
        source_permalink=PERMALINK,
        bot_user_id="USEULSEUL",
        bot_id="BSEULSEUL",
    )


def test_extract_notice_urls_supports_form_and_docs_links_only() -> None:
    text = (
        "폼 <https://form.office.naver.com/form/abc|제출> "
        "문서 https://docs.example.test/guide. 일반 https://example.test/page"
    )

    links = extract_notice_urls(text)

    assert links == (
        ("https://form.office.naver.com/form/abc", "https://form.office.naver.com/form/abc"),
        ("https://docs.example.test/guide", "https://docs.example.test/guide"),
    )


def test_canonicalize_url_removes_query_fragment_and_trailing_slash() -> None:
    assert (
        canonicalize_url("HTTPS://Forms.Example.Test/task/?utm_source=slack#section")
        == "https://forms.example.test/task"
    )


def test_message_must_be_in_configured_channel() -> None:
    event = channel_message(channel="C9999999999")

    assert not is_new_notice_message(event, {ALLOWED_CHANNEL}, "USEULSEUL")
    assert record(NoticeService({ALLOWED_CHANNEL}), event) == []


def test_thread_reply_and_own_bot_message_are_excluded() -> None:
    thread_reply = channel_message(thread_ts="1789343000.000001")
    own_user_message = channel_message(subtype="bot_message", user="USEULSEUL")
    own_bot_message = channel_message(subtype="bot_message", user=None, bot_id="BSEULSEUL")

    assert not is_new_notice_message(thread_reply, {ALLOWED_CHANNEL}, "USEULSEUL")
    assert not is_new_notice_message(own_user_message, {ALLOWED_CHANNEL}, "USEULSEUL")
    assert not is_new_notice_message(own_bot_message, {ALLOWED_CHANNEL}, "USEULSEUL", "BSEULSEUL")


def test_other_bot_message_is_allowed() -> None:
    event = channel_message(subtype="bot_message", user="UOTHERBOT", bot_id="BOTHER")

    assert is_new_notice_message(event, {ALLOWED_CHANNEL}, "USEULSEUL")


def test_message_without_matching_url_is_ignored() -> None:
    service = NoticeService({ALLOWED_CHANNEL})

    assert record(service, channel_message(text="일반 안내 https://example.test/page")) == []
    assert service.recent_notices(5) == []


def test_each_link_becomes_a_separate_notice() -> None:
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer)
    event = channel_message(
        text=("9월 20일까지 제출 https://forms.example.test/one https://docs.example.test/two")
    )

    notices = record(service, event)

    assert [notice.original_url for notice in notices] == [
        "https://forms.example.test/one",
        "https://docs.example.test/two",
    ]
    assert all(notice.workspace_id == WORKSPACE_ID for notice in notices)
    assert all(notice.source_permalink == PERMALINK for notice in notices)
    assert all(notice.processing_status == "processed" for notice in notices)
    assert [call[1] for call in analyzer.calls] == [
        "https://forms.example.test/one",
        "https://docs.example.test/two",
    ]


def test_canonical_duplicate_link_is_ignored() -> None:
    service = NoticeService({ALLOWED_CHANNEL})

    first = record(service)
    duplicate = record(
        service,
        channel_message(
            ts="1789344001.000100",
            text="9월 20일까지 제출 https://forms.example.test/task/?from=second#top",
        ),
    )

    assert len(first) == 1
    assert duplicate == []
    assert len(service.recent_notices(5)) == 1


def test_repository_keeps_duplicate_detection_across_service_instances() -> None:
    repository = FakeNoticeRepository()
    first_service = NoticeService({ALLOWED_CHANNEL}, repository=repository)
    restarted_service = NoticeService({ALLOWED_CHANNEL}, repository=repository)

    assert len(record(first_service)) == 1
    assert record(restarted_service, channel_message(ts="1789344001.000100")) == []
    assert len(repository.notices) == 1


def test_ai_failure_is_classified_without_logging_notice_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    analyzer = FakeAnalyzer(AiClientError("AI 서버 오류"))
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer)
    event = channel_message(text="홍길동 개인정보 9월 20일까지 https://forms.example.test/task")

    with caplog.at_level(logging.WARNING):
        notices = record(service, event)

    assert notices[0].processing_status == "processing_failed"
    assert notices[0].analysis is None
    assert notices[0].last_error == "AI 서버 오류"
    assert "홍길동" not in caplog.text


def test_ai_retry_count_is_passed_to_repository() -> None:
    repository = FakeNoticeRepository()
    analyzer = FakeAnalyzer(AiClientError("AI 서버 오류", retry_count=2))
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)

    record(service)

    assert repository.notices[0].processing_status == "processing_failed"
    assert repository.notices[0].retry_count == 2
    assert repository.notices[0].last_error == "AI 서버 오류"


def test_recent_notices_are_newest_first_and_bounded() -> None:
    service = NoticeService({ALLOWED_CHANNEL}, max_stored_notices=2)
    for index in range(3):
        record(
            service,
            channel_message(
                ts=f"178934400{index}.000100",
                text=f"9월 20일까지 https://forms.example.test/task-{index}",
            ),
        )

    assert [notice.original_url for notice in service.recent_notices(5)] == [
        "https://forms.example.test/task-2",
        "https://forms.example.test/task-1",
    ]


def test_recent_notices_can_be_limited_to_workspace() -> None:
    service = NoticeService({ALLOWED_CHANNEL})
    record(service)
    service.record_channel_message(
        channel_message(text="9월 20일까지 https://forms.example.test/other"),
        workspace_id="TOTHERWORKSPACE",
        source_permalink=PERMALINK,
    )

    notices = service.recent_notices(5, workspace_id=WORKSPACE_ID)

    assert len(notices) == 1
    assert notices[0].workspace_id == WORKSPACE_ID


def test_service_requires_at_least_one_channel_and_positive_limits() -> None:
    with pytest.raises(ValueError, match="채널 ID"):
        NoticeService(set())
    with pytest.raises(ValueError, match="전달된 값: 0"):
        NoticeService({ALLOWED_CHANNEL}, max_stored_notices=0)
    with pytest.raises(ValueError, match="전달된 값: -1"):
        NoticeService({ALLOWED_CHANNEL}).recent_notices(-1)


def test_notice_model_enforces_link_identity_and_tracks_ai_failures() -> None:
    table = NoticeModel.__table__
    unique_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    check_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert {
        "processing_status",
        "retry_count",
        "last_error",
        "next_retry_at",
        "deleted_at",
    } <= set(table.columns.keys())
    assert "uq_notices_workspace_canonical_url" in unique_constraints
    assert "uq_notices_source_link" in unique_constraints
    assert "ck_notices_processing_status" in check_constraints


def test_sqlalchemy_repository_writes_notice_with_analysis() -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.scalar_one_or_none.return_value = uuid4()
    repository = SqlAlchemyNoticeRepository(lambda: session)
    stored_notice = Notice(
        workspace_id=WORKSPACE_ID,
        channel_id=ALLOWED_CHANNEL,
        message_ts=MESSAGE_TS,
        text="9월 20일까지 제출",
        original_url="https://forms.example.test/task",
        canonical_url="https://forms.example.test/task",
        source_permalink=PERMALINK,
        posted_at=datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        processing_status="processed",
        analysis=FakeAnalyzer().analyze(
            "9월 20일까지 제출",
            "https://forms.example.test/task",
            datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        ),
    )

    assert repository.add(stored_notice)
    statement = session.execute.call_args.args[0]
    parameters = statement.compile().params
    assert parameters["title"] == "과제 제출"
    assert parameters["deadline_source_text"] == "9월 20일까지"
    assert parameters["processing_status"] == "processed"
    session.commit.assert_called_once_with()


def test_sqlalchemy_repository_restores_domain_notice() -> None:
    model = NoticeModel(
        workspace_id=WORKSPACE_ID,
        channel_id=ALLOWED_CHANNEL,
        message_ts=MESSAGE_TS,
        source_text="9월 20일까지 제출",
        original_url="https://forms.example.test/task",
        canonical_url="https://forms.example.test/task",
        source_permalink=PERMALINK,
        posted_at=datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        title="과제 제출",
        summary="과제를 제출합니다.",
        deadline_at=datetime(2026, 9, 20, 23, 59, tzinfo=SEOUL),
        deadline_source_text="9월 20일까지",
        processing_status="processed",
        retry_count=0,
    )
    session = MagicMock()
    session.__enter__.return_value = session
    session.execute.return_value.scalars.return_value.all.return_value = [model]
    repository = SqlAlchemyNoticeRepository(lambda: session)

    notices = repository.recent(5)

    assert notices[0].analysis is not None
    assert notices[0].analysis.title == "과제 제출"
    assert notices[0].text == "9월 20일까지 제출"
