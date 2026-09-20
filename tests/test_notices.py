"""설정 채널의 메시지에서 링크별 공지를 안전하게 수집하는지 확인한다."""

import logging
from argparse import Namespace
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from importlib import import_module
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, UniqueConstraint, create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from seulseul.ai.client import AiClientError
from seulseul.ai.model import NoticeAnalysis
from seulseul.config import AiSettings, ConfigError, DatabaseSettings, SlackSettings
from seulseul.database import Base
from seulseul.notices.events import parse_notice_event
from seulseul.notices.model import Notice, NoticeModel, NoticeSourceModel
from seulseul.notices.repository import InMemoryNoticeRepository, SqlAlchemyNoticeRepository
from seulseul.notices.retry import main as retry_main
from seulseul.notices.retry import run_command
from seulseul.notices.service import (
    NoticeRetryError,
    NoticeService,
    canonicalize_url,
    extract_notice_urls,
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


def record(service: NoticeService, event: dict[str, Any] | None = None) -> list[Notice]:
    return service.record_channel_message(
        event or channel_message(),
        workspace_id=WORKSPACE_ID,
        source_permalink=PERMALINK,
        bot_user_id="USEULSEUL",
        bot_id="BSEULSEUL",
    )


def changed_message(text: str, revision: str = "1789344100.000001", **overrides: Any) -> dict:
    event = {
        "type": "message",
        "subtype": "message_changed",
        "channel": ALLOWED_CHANNEL,
        "ts": revision,
        "event_ts": revision,
        "message": {"ts": MESSAGE_TS, "user": "UWRITER", "text": text, "edited": {"ts": revision}},
    }
    event.update(overrides)
    return event


def deleted_message(revision: str = "1789344200.000001", **overrides: Any) -> dict:
    event = {
        "type": "message",
        "subtype": "message_deleted",
        "channel": ALLOWED_CHANNEL,
        "ts": revision,
        "event_ts": revision,
        "deleted_ts": MESSAGE_TS,
    }
    event.update(overrides)
    return event


@pytest.fixture(params=["memory", "sql"])
def notice_repository(request):
    if request.param == "memory":
        yield InMemoryNoticeRepository(50)
    else:
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        try:
            yield SqlAlchemyNoticeRepository(sessionmaker(engine))
        finally:
            engine.dispose()


@pytest.mark.parametrize("kind", ["changed", "deleted"])
def test_parse_mutation_uses_original_identity_and_allows_missing_channel_type(kind):
    payload = changed_message("수정") if kind == "changed" else deleted_message()
    parsed = parse_notice_event(payload, {ALLOWED_CHANNEL}, "USEULSEUL")
    assert parsed is not None and parsed.message_ts == MESSAGE_TS
    assert parsed.kind == kind and parsed.revision == Decimal(payload["event_ts"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"channel": "COTHER"},
        {"channel_type": "im"},
        {"type": "reaction_added"},
        {"message": None},
        {"message": []},
        {"message": {"ts": MESSAGE_TS}},
        {"message": {"ts": MESSAGE_TS, "text": 123}},
        {"message": {"ts": "NaN", "text": "안내"}},
        {"message": {"ts": "999999999999.0", "text": "안내"}},
        {"message": {"ts": MESSAGE_TS, "text": "안내", "thread_ts": "1.000001"}},
        {"message": {"ts": MESSAGE_TS, "text": "안내", "user": "USEULSEUL"}},
        {"message": {"ts": MESSAGE_TS, "text": "안내", "bot_id": "BSEULSEUL"}},
    ],
)
def test_parse_mutation_rejects_malformed_out_of_scope_and_own_messages(overrides):
    assert (
        parse_notice_event(
            changed_message("수정", **overrides), {ALLOWED_CHANNEL}, "USEULSEUL", "BSEULSEUL"
        )
        is None
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"deleted_ts": "invalid"},
        {"event_ts": "NaN"},
        {"event_ts": "1.000001"},
        {"previous_message": "bad"},
        {"previous_message": {"ts": "1.000001"}},
        {"previous_message": {"ts": MESSAGE_TS, "thread_ts": "1.000001"}},
        {"previous_message": {"ts": MESSAGE_TS, "bot_id": "BSEULSEUL"}},
    ],
)
def test_parse_delete_rejects_invalid_identity_and_replies(overrides):
    assert (
        parse_notice_event(
            deleted_message(**overrides), {ALLOWED_CHANNEL}, "USEULSEUL", "BSEULSEUL"
        )
        is None
    )


def test_other_bot_edit_and_private_channel_are_allowed():
    payload = changed_message("안내", channel_type="group")
    payload["message"].update(subtype="bot_message", bot_id="BOTHER")
    assert parse_notice_event(payload, {ALLOWED_CHANNEL}, "USEULSEUL", "BSEULSEUL")


def test_changed_notice_preserves_identity_and_reanalyzes_original_posted_time(notice_repository):
    analyzer = FakeAnalyzer()
    notify = MagicMock()
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository, on_change=notify
    )
    original = record(service)[0]
    updated_analysis = replace(
        original.analysis,
        title="수정 과제",
        summary="새 안내",
        deadline_at=datetime(2026, 9, 22, 23, 59, tzinfo=SEOUL),
    )
    analyzer.analyze = MagicMock(return_value=updated_analysis)
    text = "내용 수정 9월 20일까지 https://forms.example.test/task"
    updated = record(service, changed_message(text))[0]
    assert updated.message_ts == MESSAGE_TS
    assert updated.canonical_url == original.canonical_url
    assert updated.analysis.title == "수정 과제" and updated.text == text
    assert updated.analysis.deadline_at == updated_analysis.deadline_at
    analyzer.analyze.assert_called_once()
    assert analyzer.analyze.call_args.args[2] == original.posted_at
    assert len(service.recent_notices(10)) == 1 and notify.call_count == 2


def test_link_change_adds_removes_and_restores_without_stealing_duplicates(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    original = record(service)[0]
    other = record(
        service,
        channel_message(ts="1789344001.000100", text="다른 원문 https://forms.example.test/other"),
    )[0]
    record(service, changed_message("수정 https://docs.example.test/added " + other.original_url))
    assert notice_repository.get(WORKSPACE_ID, original.canonical_url).deleted_at is not None
    assert (
        notice_repository.get(
            WORKSPACE_ID, other.canonical_url, other.channel_id, other.message_ts
        ).message_ts
        == other.message_ts
    )
    assert {n.canonical_url for n in service.recent_notices(10)} == {
        other.canonical_url,
        "https://docs.example.test/added",
    }
    record(service, changed_message(original.text, "1789344300.000001"))
    restored = notice_repository.get(WORKSPACE_ID, original.canonical_url)
    assert restored.deleted_at is None and restored.processing_status == "processed"
    assert notice_repository.get(WORKSPACE_ID, "https://docs.example.test/added").deleted_at


@pytest.mark.parametrize("text", ["", "일반 안내 https://example.test/no-task"])
def test_removing_all_links_deletes_without_ai_or_permalink(notice_repository, text):
    analyzer = FakeAnalyzer()
    notify = MagicMock()
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository, on_change=notify
    )
    original = record(service)[0]
    original = notice_repository.get(WORKSPACE_ID, original.canonical_url)
    result = service.record_channel_message(changed_message(text), workspace_id=WORKSPACE_ID)
    assert result[0].deleted_at is not None and service.recent_notices(10) == []
    assert len(analyzer.calls) == 1 and notify.call_count == 2
    assert notice_repository.get(WORKSPACE_ID, original.canonical_url).analysis == original.analysis


def test_failed_edit_keeps_analysis_stores_latest_source_and_supports_retry(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    original = record(service)[0]
    original = notice_repository.get(WORKSPACE_ID, original.canonical_url)
    analyzer.error = AiClientError("수정 분석 실패", retry_count=2)
    text = "새 마감 안내 https://forms.example.test/task"
    failed = record(service, changed_message(text))[0]
    assert failed.analysis == original.analysis and failed.text == text
    assert failed.processing_status == "processing_failed" and failed.last_error == "수정 분석 실패"
    assert failed.deleted_at is None
    analyzer.error = None
    result = service.retry_failed_notice(WORKSPACE_ID, failed.original_url)
    assert analyzer.calls[-1][0] == text and result.last_error is None


def test_ai_disabled_edit_preserves_previously_displayed_analysis(notice_repository):
    original = record(
        NoticeService({ALLOWED_CHANNEL}, analyzer=FakeAnalyzer(), repository=notice_repository)
    )[0]
    original = notice_repository.get(WORKSPACE_ID, original.canonical_url)
    disabled = NoticeService({ALLOWED_CHANNEL}, repository=notice_repository)
    result = record(disabled, changed_message("다른 내용 " + original.original_url))[0]
    assert result.analysis == original.analysis and result.processing_status == "processing_failed"
    assert "AI_PROVIDER" in result.last_error


def test_metadata_only_changes_and_repeated_events_do_not_reanalyze(notice_repository):
    analyzer = FakeAnalyzer()
    notify = MagicMock()
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository, on_change=notify
    )
    original = record(service)[0]
    event = changed_message(original.text)
    assert record(service, event) == []
    assert record(service, event) == []
    assert len(analyzer.calls) == 1 and notify.call_count == 1


def test_delete_is_terminal_scoped_and_persisted_across_service_restart(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    original = record(service)[0]
    foreign = service.record_channel_message(
        channel_message(), workspace_id="TOTHER", source_permalink=PERMALINK
    )[0]
    record(service, deleted_message())
    restarted = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    assert record(restarted) == []
    assert record(restarted, changed_message(original.text, "1789344500.000001")) == []
    assert record(restarted, deleted_message()) == []
    assert notice_repository.get(WORKSPACE_ID, original.canonical_url).deleted_at
    assert notice_repository.get("TOTHER", foreign.canonical_url).deleted_at is None
    assert len(analyzer.calls) == 2


def test_delete_before_create_leaves_tombstone_even_without_notice(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    assert record(service, deleted_message()) == []
    assert record(service) == []
    assert service.recent_notices(10) == [] and analyzer.calls == []


def test_newer_edit_wins_and_older_creation_cannot_remove_added_links(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    latest = "최신 https://forms.example.test/latest"
    record(service, changed_message(latest, "1789344400.000001"))
    assert record(service, changed_message("오래된 https://forms.example.test/old")) == []
    assert record(service) == []
    assert service.recent_notices(10)[0].text == latest and len(analyzer.calls) == 1


@pytest.mark.parametrize("mutation", ["delete", "newer_edit"])
def test_in_flight_analysis_cannot_overwrite_newer_event(notice_repository, mutation):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    original = record(service)[0]
    slow_text = "느린 변경 " + original.original_url
    latest_text = "최신 변경 " + original.original_url
    delegate = analyzer.analyze

    def analyze(text, url, posted_at):
        if text == slow_text:
            event = (
                deleted_message()
                if mutation == "delete"
                else changed_message(latest_text, "1789344500.000001")
            )
            record(service, event)
        return delegate(text, url, posted_at)

    analyzer.analyze = analyze
    assert record(service, changed_message(slow_text)) == []
    latest = notice_repository.get(WORKSPACE_ID, original.canonical_url)
    if mutation == "delete":
        assert latest.deleted_at is not None
    else:
        assert latest.text == latest_text


def test_retry_during_pending_edit_is_rejected(notice_repository):
    original = failed_notice()
    notice_repository.add(original)
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=FakeAnalyzer(), repository=notice_repository
    )
    event = service.parse_event(changed_message("변경 " + original.original_url), None)
    assert notice_repository.begin_event(WORKSPACE_ID, event)
    with pytest.raises(NoticeRetryError, match="상태가 변경"):
        service.retry_failed_notice(WORKSPACE_ID, original.original_url)


def test_sql_source_state_survives_repository_recreation():
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        service = NoticeService({ALLOWED_CHANNEL}, repository=SqlAlchemyNoticeRepository(factory))
        record(service, deleted_message())
        restarted = NoticeService({ALLOWED_CHANNEL}, repository=SqlAlchemyNoticeRepository(factory))
        assert record(restarted) == []
        with factory() as session:
            source = session.get(NoticeSourceModel, (WORKSPACE_ID, ALLOWED_CHANNEL, MESSAGE_TS))
            assert source.deleted and source.applied
            assert source.revision == Decimal("1789344200.000001")
    finally:
        engine.dispose()


def test_deletion_wins_same_revision_even_if_edit_analysis_is_pending(notice_repository):
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=FakeAnalyzer(), repository=notice_repository
    )
    original = record(service)[0]
    edit = service.parse_event(changed_message("수정 " + original.original_url), None)
    assert notice_repository.begin_event(WORKSPACE_ID, edit)
    deletion = service.parse_event(deleted_message("1789344100.000001"), None)
    assert notice_repository.begin_event(WORKSPACE_ID, deletion)
    assert notice_repository.apply_event(WORKSPACE_ID, edit, [original]) is None
    assert notice_repository.apply_event(WORKSPACE_ID, deletion, [])[0].deleted_at is not None


def test_failed_transaction_can_replay_same_event_without_partial_changes(monkeypatch):
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        repository = SqlAlchemyNoticeRepository(factory)
        service = NoticeService({ALLOWED_CHANNEL}, analyzer=FakeAnalyzer(), repository=repository)
        original = record(service)[0]
        with monkeypatch.context() as scoped:
            scoped.setattr(
                "seulseul.notices.repository.set_notice_checklists_deleted",
                MagicMock(side_effect=RuntimeError("DB failure")),
            )
            with pytest.raises(RuntimeError, match="DB failure"):
                record(service, deleted_message())
        assert repository.get(WORKSPACE_ID, original.canonical_url).deleted_at is None
        assert record(service, deleted_message())[0].deleted_at is not None
    finally:
        engine.dispose()


def test_source_migration_preserves_existing_notices_and_backfills_once():
    migration = import_module("migrations.versions.c83021fb573d_공지_원본_이벤트_상태")
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            Base.metadata.create_all(
                connection,
                tables=[
                    table for table in Base.metadata.sorted_tables if table.name != "notice_sources"
                ],
            )
            connection.execute(
                NoticeModel.__table__.insert(),
                [
                    {
                        "workspace_id": WORKSPACE_ID,
                        "channel_id": ALLOWED_CHANNEL,
                        "message_ts": MESSAGE_TS,
                        "source_text": "기존 원문",
                        "original_url": f"https://forms.example.test/{suffix}",
                        "canonical_url": f"https://forms.example.test/{suffix}",
                        "source_permalink": PERMALINK,
                        "posted_at": datetime(2026, 9, 14),
                        "processing_status": "ai_disabled",
                    }
                    for suffix in ("one", "two")
                ],
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            sources = connection.execute(select(NoticeSourceModel.__table__)).mappings().all()
            assert len(sources) == 1 and sources[0]["applied"] and not sources[0]["deleted"]
            assert sources[0]["revision"] == Decimal(MESSAGE_TS)
            assert len(connection.execute(select(NoticeModel.id)).all()) == 2
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "notice_sources" not in inspect(connection).get_table_names()
            assert len(connection.execute(select(NoticeModel.id)).all()) == 2
    finally:
        engine.dispose()


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

    assert parse_notice_event(event, {ALLOWED_CHANNEL}, "USEULSEUL") is None
    assert record(NoticeService({ALLOWED_CHANNEL}), event) == []


def test_thread_reply_and_own_bot_message_are_excluded() -> None:
    thread_reply = channel_message(thread_ts="1789343000.000001")
    own_user_message = channel_message(subtype="bot_message", user="USEULSEUL")
    own_bot_message = channel_message(subtype="bot_message", user=None, bot_id="BSEULSEUL")

    assert parse_notice_event(thread_reply, {ALLOWED_CHANNEL}, "USEULSEUL") is None
    assert parse_notice_event(own_user_message, {ALLOWED_CHANNEL}, "USEULSEUL") is None
    assert parse_notice_event(own_bot_message, {ALLOWED_CHANNEL}, "USEULSEUL", "BSEULSEUL") is None


def test_other_bot_message_is_allowed() -> None:
    event = channel_message(subtype="bot_message", user="UOTHERBOT", bot_id="BOTHER")

    assert parse_notice_event(event, {ALLOWED_CHANNEL}, "USEULSEUL").kind == "created"


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


def test_canonical_duplicate_link_in_another_source_is_stored() -> None:
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
    assert len(duplicate) == 1
    assert duplicate[0].canonical_url == first[0].canonical_url
    assert len(service.recent_notices(5)) == 2


def test_cross_channel_sources_are_independent_and_retry_requires_source(notice_repository):
    service = NoticeService(
        {ALLOWED_CHANNEL, "CCLASS1"}, analyzer=FakeAnalyzer(), repository=notice_repository
    )
    first = record(service)[0]
    second_event = channel_message(channel="CCLASS1", ts="1789344001.000100")
    second = record(service, second_event)[0]
    assert first.canonical_url == second.canonical_url
    assert len(service.recent_notices(10)) == 2
    assert record(service, second_event) == []
    with pytest.raises(NoticeRetryError, match="원본이 여러"):
        service.retry_failed_notice(WORKSPACE_ID, first.original_url)
    record(service, deleted_message())
    assert (
        notice_repository.get(
            WORKSPACE_ID, second.canonical_url, second.channel_id, second.message_ts
        ).deleted_at
        is None
    )


def test_retry_selects_only_one_of_two_failed_sources(notice_repository):
    failed = NoticeService(
        {ALLOWED_CHANNEL, "CCLASS1"},
        analyzer=FakeAnalyzer(AiClientError("failed")),
        repository=notice_repository,
    )
    first = record(failed)[0]
    second = record(failed, channel_message(channel="CCLASS1", ts="1789344001.000100"))[0]
    service = NoticeService(
        {ALLOWED_CHANNEL, "CCLASS1"}, analyzer=FakeAnalyzer(), repository=notice_repository
    )
    result = service.retry_failed_notice(
        WORKSPACE_ID, second.original_url, second.channel_id, second.message_ts
    )
    assert result.processing_status == "processed"
    assert (
        notice_repository.get(
            WORKSPACE_ID, first.canonical_url, first.channel_id, first.message_ts
        ).processing_status
        == "processing_failed"
    )


def test_similar_surveys_with_distinct_links_are_both_collected(notice_repository):
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=notice_repository)
    for ts, course, link, deadline in [
        (MESSAGE_TS, "모델 개발", "https://forms.example.test/model", "9/27(일)"),
        ("1789344001.000100", "스프링 개발", "https://forms.example.test/spring", "9/26(토)"),
    ]:
        results = record(
            service,
            channel_message(
                ts=ts,
                text=(
                    f"<!here> {course} 교과목 종료 서베이 <{link}>에 참여해주세요!\n"
                    f"※ 서베이는 익명으로 진행됩니다.\n※ 마감일 : ~{deadline}"
                ),
            ),
        )
        assert len(results) == 1 and results[0].original_url == link
        assert results[0].processing_status == "processed"
    assert len(analyzer.calls) == 2
    assert {notice.canonical_url for notice in service.recent_notices(5)} == {
        "https://forms.example.test/model",
        "https://forms.example.test/spring",
    }


def test_repository_keeps_duplicate_detection_across_service_instances() -> None:
    repository = InMemoryNoticeRepository(10)
    first_service = NoticeService({ALLOWED_CHANNEL}, repository=repository)
    restarted_service = NoticeService({ALLOWED_CHANNEL}, repository=repository)

    assert len(record(first_service)) == 1
    assert record(restarted_service) == []
    assert len(repository.recent(10)) == 1


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
    repository = InMemoryNoticeRepository(10)
    analyzer = FakeAnalyzer(AiClientError("AI 서버 오류", retry_count=2))
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)

    record(service)

    assert repository.recent(10)[0].processing_status == "processing_failed"
    assert repository.recent(10)[0].retry_count == 2
    assert repository.recent(10)[0].last_error == "AI 서버 오류"


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
    assert "uq_notices_workspace_canonical_url" not in unique_constraints
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


def failed_notice(**overrides: Any) -> Notice:
    notice = Notice(
        workspace_id=WORKSPACE_ID,
        channel_id=ALLOWED_CHANNEL,
        message_ts=MESSAGE_TS,
        text="개인정보 포함 원문 9월 20일까지 제출",
        original_url="https://forms.example.test/task?source=slack",
        canonical_url="https://forms.example.test/task",
        source_permalink=PERMALINK,
        posted_at=datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        processing_status="processing_failed",
        retry_count=2,
        last_error="이전 연결 오류",
        next_retry_at=datetime(2026, 9, 15, 9, 0, tzinfo=SEOUL),
    )
    return replace(notice, **overrides)


def test_successful_new_notice_wakes_worker_once_after_storage() -> None:
    changes = []
    service = NoticeService(
        {ALLOWED_CHANNEL},
        analyzer=FakeAnalyzer(),
        on_change=lambda: changes.append(service.recent_notices(5)),
    )
    record(service)
    record(service)
    assert len(changes) == 1
    assert changes[0][0].processing_status == "processed"


def test_retry_success_wakes_worker_without_changing_link_identity() -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    changed = MagicMock()
    service = NoticeService(
        {ALLOWED_CHANNEL}, analyzer=FakeAnalyzer(), repository=repository, on_change=changed
    )
    service.retry_failed_notice(WORKSPACE_ID, original.original_url)
    changed.assert_called_once_with()
    assert repository.recent(5)[0].canonical_url == original.canonical_url


def test_retry_updates_existing_notice_and_preserves_original_input() -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)

    result = service.retry_failed_notice(WORKSPACE_ID, original.canonical_url + "/#section")

    assert result.processing_status == "processed"
    assert result.analysis is not None
    assert result.last_error is None
    assert result.next_retry_at is None
    assert result.retry_count == 0
    assert analyzer.calls == [(original.text, original.original_url, original.posted_at)]
    assert service.recent_notices(10) == [result]
    assert service.failed_notices(10) == []
    assert record(service) == []
    with pytest.raises(NoticeRetryError, match="실패한 공지만"):
        service.retry_failed_notice(WORKSPACE_ID, original.original_url)
    assert len(analyzer.calls) == 1


def test_retry_failure_updates_error_but_preserves_previous_analysis() -> None:
    repository = InMemoryNoticeRepository(10)
    previous_analysis = FakeAnalyzer().analyze("원문", "링크", failed_notice().posted_at)
    original = failed_notice(analysis=previous_analysis)
    repository.add(original)
    service = NoticeService(
        {ALLOWED_CHANNEL},
        analyzer=FakeAnalyzer(AiClientError("새 오류", retry_count=1)),
        repository=repository,
    )

    result = service.retry_failed_notice(WORKSPACE_ID, original.original_url)

    assert result.processing_status == "processing_failed"
    assert result.last_error == "새 오류"
    assert result.retry_count == 1
    assert result.analysis == previous_analysis
    assert result.next_retry_at is None
    assert service.recent_notices(10) == [result]


@pytest.mark.parametrize(
    "overrides",
    [
        {"deleted_at": datetime(2026, 9, 16, tzinfo=SEOUL)},
        {"processing_status": "processed"},
        {"processing_status": "ai_disabled"},
        {"workspace_id": "TOTHER"},
        {"channel_id": "COTHER"},
    ],
)
def test_retry_rejects_ineligible_notice_without_ai_call(overrides: dict[str, Any]) -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice(**overrides)
    repository.add(original)
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)

    with pytest.raises(NoticeRetryError):
        service.retry_failed_notice(WORKSPACE_ID, original.original_url)

    assert analyzer.calls == []
    assert repository.get(original.workspace_id, original.canonical_url) == original


def test_retry_with_ai_disabled_leaves_failure_unchanged() -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    service = NoticeService({ALLOWED_CHANNEL}, repository=repository)

    with pytest.raises(NoticeRetryError, match="AI_PROVIDER"):
        service.retry_failed_notice(WORKSPACE_ID, original.original_url)
    assert service.failed_notices(10) == [original]


@pytest.mark.parametrize("url", ["not-a-url", "https://[", "ftp://forms.example.test/task"])
def test_retry_rejects_invalid_url(url: str) -> None:
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=FakeAnalyzer())
    with pytest.raises(NoticeRetryError, match="제출 링크"):
        service.retry_failed_notice(WORKSPACE_ID, url)


def test_failed_list_filters_before_applying_limit() -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    repository.add(failed_notice(workspace_id="TOTHER"))
    repository.add(
        failed_notice(
            canonical_url="https://forms.example.test/processed", processing_status="processed"
        )
    )
    repository.add(
        failed_notice(
            canonical_url="https://forms.example.test/deleted", deleted_at=original.posted_at
        )
    )
    repository.add(
        failed_notice(canonical_url="https://forms.example.test/channel", channel_id="COTHER")
    )
    service = NoticeService({ALLOWED_CHANNEL}, repository=repository)

    assert service.failed_notices(1, workspace_id=WORKSPACE_ID) == [original]
    with pytest.raises(ValueError, match="1 이상"):
        service.failed_notices(0)


def test_retry_does_not_overwrite_a_concurrent_change() -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    concurrent = replace(original, processing_status="processed")
    analyzer = MagicMock()

    def analyze(*args: Any) -> NoticeAnalysis:
        assert repository.replace_failed(original, concurrent)
        raise AiClientError("늦게 도착한 실패")

    analyzer.analyze.side_effect = analyze
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)
    with pytest.raises(NoticeRetryError, match="상태가 변경"):
        service.retry_failed_notice(WORKSPACE_ID, original.original_url)
    assert service.recent_notices(10) == [concurrent]


def test_sqlalchemy_retry_updates_same_row_and_rejects_stale_failure() -> None:
    # DB 저장·조회·조건부 UPDATE는 격리된 메모리 DB로 검증한다. 실제 API/DB는 쓰지 않는다.
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        with factory() as session:
            original = failed_notice()
            model = NoticeModel(
                workspace_id=original.workspace_id,
                channel_id=original.channel_id,
                message_ts=original.message_ts,
                source_text=original.text,
                original_url=original.original_url,
                canonical_url=original.canonical_url,
                source_permalink=original.source_permalink,
                posted_at=original.posted_at,
                processing_status=original.processing_status,
                retry_count=original.retry_count,
                last_error=original.last_error,
                next_retry_at=original.next_retry_at,
            )
            session.add(model)
            session.commit()
            notice_id = model.id
        repository = SqlAlchemyNoticeRepository(factory)
        stored = repository.get(WORKSPACE_ID, original.canonical_url)
        assert stored is not None
        assert repository.get("TOTHER", original.canonical_url) is None
        assert repository.failed(5, {ALLOWED_CHANNEL}, WORKSPACE_ID) == [stored]
        assert repository.failed(5, {"COTHER"}, WORKSPACE_ID) == []
        assert repository.failed(5, {ALLOWED_CHANNEL}, "TOTHER") == []
        processed = replace(
            stored,
            processing_status="processed",
            last_error=None,
            retry_count=0,
            next_retry_at=None,
            analysis=FakeAnalyzer().analyze(stored.text, stored.original_url, stored.posted_at),
        )

        assert repository.replace_failed(stored, processed)
        assert not repository.replace_failed(stored, replace(stored, last_error="늦은 오류"))
        assert repository.failed(5, {ALLOWED_CHANNEL}) == []
        restored = repository.get(WORKSPACE_ID, original.canonical_url)
        assert restored is not None and restored.processing_status == "processed"
        assert restored.analysis is not None and restored.analysis.title == "과제 제출"
        assert restored.last_error is None and restored.next_retry_at is None
        with factory() as session:
            assert session.execute(select(NoticeModel.id)).scalars().all() == [notice_id]
    finally:
        engine.dispose()


def test_sqlalchemy_retry_update_is_scoped_and_preserves_source() -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = None
    session.execute.return_value.scalar_one_or_none.return_value = uuid4()
    repository = SqlAlchemyNoticeRepository(lambda: session)
    original = failed_notice()

    assert repository.replace_failed(original, replace(original, last_error="새 오류"))

    statement = session.execute.call_args.args[0]
    sql = str(statement)
    where = sql.split(" WHERE ")[1]
    for column in (
        "workspace_id",
        "canonical_url",
        "channel_id",
        "message_ts",
        "source_text",
        "posted_at",
        "processing_status",
        "deleted_at",
        "last_error",
        "retry_count",
    ):
        assert f"notices.{column}" in where
    assigned = sql.split(" SET ")[1].split(" WHERE ")[0]
    assigned_columns = {assignment.split("=")[0] for assignment in assigned.split(", ")}
    assert "source_text" not in assigned_columns
    assert "canonical_url" not in assigned_columns
    assert statement.compile().params["last_error"] == "새 오류"
    session.commit.assert_called_once_with()


def test_retry_cli_list_prints_command_without_calling_ai_or_exposing_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice(last_error="상태 코드: 500, 응답: 비공개 원문 nvapi-secret")
    repository.add(original)
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer, repository=repository)

    assert run_command(service, Namespace(action="list", workspace_id=None, limit=20)) == 0
    output = capsys.readouterr().out
    assert "seulseul.notices.retry retry" in output
    assert WORKSPACE_ID in output and original.original_url in output
    assert "상태 코드: 500" in output
    assert "비공개 원문" not in output and "nvapi-secret" not in output
    assert original.text not in output
    assert analyzer.calls == []


@pytest.mark.parametrize("failure", [False, True])
def test_retry_cli_reports_result_and_exit_code(
    failure: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = InMemoryNoticeRepository(10)
    original = failed_notice()
    repository.add(original)
    service = NoticeService(
        {ALLOWED_CHANNEL},
        repository=repository,
        analyzer=FakeAnalyzer(AiClientError("연결 오류") if failure else None),
    )
    exit_code = run_command(
        service, Namespace(action="retry", workspace_id=WORKSPACE_ID, url=original.original_url)
    )
    assert exit_code == int(failure)
    assert ("재처리 실패" if failure else "재처리 성공") in capsys.readouterr().out


@pytest.mark.parametrize("limit", ["0", "101", "wrong"])
def test_retry_cli_rejects_invalid_limit_before_loading_settings(limit: str) -> None:
    with pytest.raises(SystemExit) as error:
        retry_main(["list", "--limit", limit])
    assert error.value.code == 2


def test_retry_missing_notice_never_calls_ai() -> None:
    analyzer = FakeAnalyzer()
    service = NoticeService({ALLOWED_CHANNEL}, analyzer=analyzer)
    with pytest.raises(NoticeRetryError, match="찾을 수 없습니다"):
        service.retry_failed_notice(WORKSPACE_ID, "https://forms.example.test/missing")
    assert analyzer.calls == []


@pytest.mark.parametrize("mode", ["list", "retry", "disabled", "config_error"])
def test_retry_cli_wires_dependencies_and_closes_resources(
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prefix = "seulseul.notices.retry."
    engine = MagicMock()
    client = MagicMock()
    client_constructor = MagicMock(return_value=client)
    service = MagicMock()
    service.failed_notices.return_value = []
    service.retry_failed_notice.return_value = failed_notice(processing_status="processed")
    service_constructor = MagicMock(return_value=service)
    monkeypatch.setattr(
        prefix + "load_slack_settings",
        lambda: SlackSettings(
            bot_token="xoxb-test",
            app_token="xapp-test",
            notice_channels=(ALLOWED_CHANNEL,),
        ),
    )
    monkeypatch.setattr(prefix + "load_database_settings", lambda: DatabaseSettings("unused"))
    monkeypatch.setattr(prefix + "create_database_engine", lambda settings: engine)
    monkeypatch.setattr(prefix + "create_session_factory", MagicMock())
    monkeypatch.setattr(prefix + "SqlAlchemyNoticeRepository", MagicMock())
    monkeypatch.setattr(prefix + "OpenAICompatibleChatClient", client_constructor)
    monkeypatch.setattr(prefix + "NoticeService", service_constructor)
    settings_loader = MagicMock(
        return_value=AiSettings(
            provider="ollama",
            base_url="http://localhost:11434/v1",
            api_key="ollama",
            model="llama3.2",
            timeout_seconds=45,
        )
    )
    if mode == "disabled":
        settings_loader.return_value = None
    elif mode == "config_error":
        settings_loader.side_effect = ConfigError("NVIDIA_MODEL이 비어 있습니다.")
    monkeypatch.setattr(prefix + "load_ai_settings", settings_loader)

    args = (
        ["list"]
        if mode == "list"
        else [
            "retry",
            "--workspace-id",
            WORKSPACE_ID,
            "--url",
            failed_notice().original_url,
        ]
    )
    assert retry_main(args) == (1 if mode in {"disabled", "config_error"} else 0)

    engine.dispose.assert_called_once_with()
    if mode == "list":
        settings_loader.assert_not_called()
        client_constructor.assert_not_called()
        service.retry_failed_notice.assert_not_called()
        assert "실패 공지가 없습니다" in capsys.readouterr().out
    elif mode == "retry":
        client.close.assert_called_once_with()
        assert client_constructor.call_args.kwargs["disable_thinking"] is False
        service.retry_failed_notice.assert_called_once_with(
            WORKSPACE_ID,
            failed_notice().original_url,
            None,
            None,
        )
    else:
        client_constructor.assert_not_called()
        service_constructor.assert_not_called()
        assert "설정" in capsys.readouterr().out or mode == "config_error"
