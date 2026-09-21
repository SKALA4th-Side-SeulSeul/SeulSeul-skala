"""체크리스트 ORM 모델의 관계와 상태 컬럼을 확인한다."""

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint, create_engine, delete, event, select
from sqlalchemy.orm import configure_mappers, sessionmaker

from seulseul.ai.client import AiClientError
from seulseul.ai.model import NoticeAnalysis
from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistModel,
    DailyChecklistMessageModel,
)
from seulseul.checklists.repository import (
    SqlAlchemyChecklistRepository,
    set_notice_checklists_deleted,
)
from seulseul.checklists.service import DailyChecklistService
from seulseul.database import Base
from seulseul.notices.model import NoticeModel
from seulseul.notices.repository import SqlAlchemyNoticeRepository
from seulseul.notices.service import NoticeService
from seulseul.users.model import StudentModel


def test_checklist_model_links_student_and_notice_with_cascade_delete() -> None:
    configure_mappers()
    table = ChecklistModel.__table__
    foreign_keys = {
        foreign_key.target_fullname: foreign_key.ondelete for foreign_key in table.foreign_keys
    }
    unique_constraints = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert foreign_keys == {"students.id": "CASCADE", "notices.id": "CASCADE"}
    assert "uq_checklists_student_notice" in unique_constraints
    assert "uq_checklists_student_link" in unique_constraints
    assert not table.c.canonical_url.nullable
    assert {"completed_at", "deleted_at"} <= set(table.columns.keys())
    assert StudentModel.checklists.property.back_populates == "student"
    assert NoticeModel.checklists.property.back_populates == "notice"


NOW = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)  # 서울 오전 9시
WORKSPACE = "TTEST"
TARGETS = {"CALL": None, "CCLASS3": 3}


class RecordingMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.updated: list[tuple] = []
        self.error: ChecklistDeliveryError | None = None

    def send(self, user_id, board):
        if self.error:
            raise self.error
        self.sent.append((user_id, board))
        return f"D{user_id}", f"100.{len(self.sent)}"

    def update(self, channel_id, message_ts, board):
        if self.error:
            raise self.error
        self.updated.append((channel_id, message_ts, board))

    def delete_previous_messages(self, workspace_id, user_id):
        if self.error:
            raise self.error


@pytest.fixture
def daily_system():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    repository = SqlAlchemyChecklistRepository(factory)
    messenger = RecordingMessenger()
    clock = [NOW]
    service = DailyChecklistService(
        repository, messenger, WORKSPACE, TARGETS, clock=lambda: clock[0]
    )
    yield factory, repository, messenger, clock, service
    engine.dispose()


def test_command_reset_replaces_dm_but_preserves_completion_and_other_students(daily_system):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_student(factory, user="UTWO")
    add_notice(factory)
    service.run_due()
    with factory() as session, session.begin():
        item = session.scalar(select(ChecklistModel).where(ChecklistModel.student_id == student_id))
        item.completed_at = NOW
    with service.reset_messages(WORKSPACE, "UONE"):
        assert len(get_daily(factory)) == 2
    assert len(get_daily(factory)) == 1
    service.run_due()
    assert len(messenger.sent) == 3
    assert messenger.sent[-1][1].completed_count == 1
    assert len(get_daily(factory)) == 2


def test_interactive_manual_edit_preserves_completion_and_updates_same_dm(daily_system):
    from seulseul.notices.manual import build_manual_event
    from seulseul.notices.manual_edit import run_interactive

    factory, _, messenger, _, daily_service = daily_system
    add_student(factory)
    notices = NoticeService({"CALL"}, repository=SqlAlchemyNoticeRepository(factory))
    message_ts = f"{int(NOW.timestamp())}.000100"
    permalink = f"https://workspace.slack.com/archives/CALL/p{message_ts.replace('.', '')}"
    notices.record_channel_message(
        build_manual_event("CALL", message_ts, "https://forms.example.test/task", kind="created"),
        workspace_id=WORKSPACE,
        source_permalink=permalink,
        manual_analysis=NoticeAnalysis("원래 제목", "요약", NOW + timedelta(days=1), "내일"),
    )
    daily_service.run_due()
    original_dm = get_daily(factory)[0]
    item_id = messenger.sent[0][1].items[0].id
    click(daily_service, original_dm, "complete", item_id)
    answers = iter(["1", "수정 제목", "", "", "", "y"])
    assert run_interactive(notices, read=lambda prompt: next(answers)) == 0
    daily_service.run_due()
    click(daily_service, original_dm, "completed")
    with factory() as session:
        assert session.get(ChecklistModel, item_id).completed_at is not None
    assert len(messenger.sent) == 1
    assert messenger.updated[-1][:2] == (original_dm.dm_channel_id, original_dm.message_ts)
    assert messenger.updated[-1][2].items[0].title == "수정 제목"


def test_withdraw_cleanup_does_not_send_replacement(daily_system):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_notice(factory)
    service.run_due()
    with service.reset_messages(WORKSPACE, "UONE"):
        with factory() as session, session.begin():
            session.execute(delete(StudentModel).where(StudentModel.id == student_id))
    service.run_due()
    assert len(messenger.sent) == 1
    assert get_daily(factory) == []
    with factory() as session:
        assert session.scalar(select(ChecklistModel)) is None


@pytest.mark.parametrize("already_sent", [False, True])
def test_failed_cleanup_stays_paused_after_restart_until_command_retried(
    daily_system, already_sent
):
    factory, repository, messenger, clock, service = daily_system
    add_student(factory)
    if already_sent:
        service.run_due()
    messenger.error = ChecklistDeliveryError("missing_scope")
    with pytest.raises(ChecklistDeliveryError), service.reset_messages(WORKSPACE, "UONE"):
        pytest.fail("cleanup failure must not continue enrollment or withdrawal")
    assert get_daily(factory)[0].last_error == "dm_cleanup_pending"
    messenger.error = None
    restarted = DailyChecklistService(
        repository, messenger, WORKSPACE, TARGETS, clock=lambda: clock[0]
    )
    restarted.run_due()
    assert len(messenger.sent) == int(already_sent)
    with restarted.reset_messages(WORKSPACE, "UONE"):
        pass
    restarted.run_due()
    assert len(messenger.sent) == int(already_sent) + 1


def add_student(factory, user="UONE", class_number=3, workspace=WORKSPACE):
    with factory() as session, session.begin():
        model = StudentModel(
            workspace_id=workspace,
            slack_user_id=user,
            real_name=f"4기_광주_{class_number}반_가상학생",
            campus="광주",
            class_number=class_number,
        )
        session.add(model)
        session.flush()
        return model.id


def add_notice(factory, **overrides: Any):
    values = dict(
        workspace_id=WORKSPACE,
        channel_id="CALL",
        message_ts=str(uuid4()),
        source_text="화면에 노출하지 않을 원문",
        original_url="https://forms.example.test/a",
        canonical_url=f"https://forms.example.test/{uuid4()}",
        source_permalink="https://workspace.slack.com/archives/C1/p123",
        posted_at=NOW - timedelta(days=1),
        deadline_at=NOW + timedelta(days=2),
        title="과제 제출",
        summary="폼으로 제출하세요.",
        deadline_source_text="모레까지",
        processing_status="processed",
    )
    values.update(overrides)
    with factory() as session, session.begin():
        model = NoticeModel(**values)
        session.add(model)
        session.flush()
        return model.id


def test_cross_class_links_and_all_channel_keep_one_item_and_completion(daily_system):
    factory, repository, messenger, clock, _ = daily_system
    targets = {"CALL": None, "CCLASS1": 1, "CCLASS3": 3}
    service = DailyChecklistService(
        repository, messenger, WORKSPACE, targets, clock=lambda: clock[0]
    )
    for number in range(1, 5):
        add_student(factory, user=f"USTUDENT{number}", class_number=number)
    links = ["https://forms.example.test/shared", "https://docs.example.test/shared"]
    third = [
        add_notice(
            factory,
            channel_id="CCLASS3",
            message_ts="1789559318.987269",
            canonical_url=url,
            original_url=url,
            title="3반 안내",
        )
        for url in links
    ]
    service.run_due()

    def board(user):
        recipient = repository.recipient(WORKSPACE, user)
        claim = repository.prepare_delivery(
            recipient,
            service._channels(recipient),
            clock[0],
            class_channels=[
                channel for channel, target in targets.items() if target == recipient.class_number
            ],
        )
        assert claim is not None
        repository.finish_delivery(claim, "test", f"D{user}", "100.1")
        return claim.board

    assert board("USTUDENT3").pending_count == 2
    assert board("USTUDENT1").pending_count == 0
    source_refs = {item.source_ref for item in board("USTUDENT3").items}
    assert len(source_refs) == 1 and next(iter(source_refs)).startswith("CCLASS3:")
    first = [
        add_notice(
            factory,
            channel_id="CCLASS1",
            message_ts="1789559319.987269",
            canonical_url=url,
            original_url=url,
            title="1반 안내",
        )
        for url in links
    ]
    assert board("USTUDENT1").pending_count == 2
    assert board("USTUDENT2").pending_count == 0
    before = board("USTUDENT1")
    item_id = before.items[0].id
    with factory() as session, session.begin():
        session.get(ChecklistModel, item_id).completed_at = NOW
    # 전체 공지가 더 늦게 올라와도 해당 반 원문을 우선한다.
    for url in links:
        add_notice(factory, canonical_url=url, original_url=url, title="전체 안내", posted_at=NOW)
    one = board("USTUDENT1")
    assert one.pending_count == 1 and one.completed_count == 1
    assert all(item.title == "1반 안내" for item in one.items)
    assert board("USTUDENT2").pending_count == 2
    assert board("USTUDENT4").pending_count == 2
    assert all(item.title == "3반 안내" for item in board("USTUDENT3").items)
    # 1반 원본이 없어지면 전체 공지로 전환, 완료 ID·기록은 유지한다.
    with factory() as session, session.begin():
        for notice_id in first:
            session.get(NoticeModel, notice_id).deleted_at = NOW
        set_notice_checklists_deleted(session, first, NOW)
    one = board("USTUDENT1")
    assert one.pending_count == 1 and one.completed_count == 1
    assert all(item.title == "전체 안내" for item in one.items)
    with factory() as session:
        assert session.get(ChecklistModel, item_id).completed_at is not None
        assert len(session.scalars(select(ChecklistModel)).all()) == 8
    # 남은 전체 원본도 삭제하면 1반에서 숨기고 3반은 그대로 둔다.
    with factory() as session, session.begin():
        for notice in session.scalars(select(NoticeModel).where(NoticeModel.channel_id == "CALL")):
            notice.deleted_at = NOW
    assert board("USTUDENT1").pending_count == 0
    assert board("USTUDENT1").completed_count == 0
    assert board("USTUDENT3").pending_count == 2
    assert len(third) == 2


@pytest.mark.parametrize("specific_first", [True, False])
def test_specific_source_wins_in_both_arrival_orders_with_own_deadline(
    daily_system, specific_first
):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    shared = "https://forms.example.test/common"
    sources = [
        ("CCLASS3", "반 원문", NOW + timedelta(days=1)),
        ("CALL", "전체 원문", NOW + timedelta(days=3)),
    ]
    if not specific_first:
        sources.reverse()
    stable_id = None
    for channel, title, deadline in sources:
        add_notice(
            factory, channel_id=channel, title=title, deadline_at=deadline, canonical_url=shared
        )
        service.run_due()
        with factory() as session:
            item = session.scalar(select(ChecklistModel))
            if stable_id is None:
                stable_id = item.id
            assert item.id == stable_id
    current = messenger.updated[-1][2] if messenger.updated else messenger.sent[-1][1]
    assert current.pending_count == 1
    assert current.items[0].title == "반 원문"
    assert current.items[0].deadline_at == NOW + timedelta(days=1)


def test_newer_same_priority_source_switch_preserves_completion_and_undo(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    original_id = add_notice(factory, canonical_url="https://forms.example.test/shared")
    service.run_due()
    original_board = messenger.sent[-1][1]
    item_id = original_board.items[0].id
    click(service, get_daily(factory)[0], "complete", item_id)
    newer_id = add_notice(
        factory, canonical_url="https://forms.example.test/shared", posted_at=NOW, title="최신 원본"
    )
    service.run_due()
    with factory() as session:
        item = session.get(ChecklistModel, item_id)
        assert item.notice_id == newer_id and item.completed_at is not None
    click(service, get_daily(factory)[0], "completed")
    assert messenger.updated[-1][2].items[0].title == "최신 원본"
    click(service, get_daily(factory)[0], "undo", item_id)
    with factory() as session:
        assert session.get(ChecklistModel, item_id).completed_at is None
    with factory() as session, session.begin():
        session.get(NoticeModel, newer_id).deleted_at = NOW
    service.run_due()
    with factory() as session:
        item = session.get(ChecklistModel, item_id)
        assert item.notice_id == original_id and item.completed_at is None


def get_daily(factory):
    with factory() as session:
        return session.execute(select(DailyChecklistMessageModel)).scalars().all()


def click(service, daily, operation, item=None, *, user="UONE", workspace=WORKSPACE):
    value = {"daily": str(daily.id)}
    if item is not None:
        value["item"] = str(item)
    return service.handle_action(
        workspace, user, daily.dm_channel_id, daily.message_ts, operation, json.dumps(value)
    )


def test_source_edits_link_removal_restoration_and_delete_update_same_dm(daily_system):
    factory, _, messenger, _, daily_service = daily_system
    add_student(factory)

    class Analyzer:
        fail = False

        def analyze(self, text, url, posted_at):
            if self.fail:
                raise AiClientError("분석 실패")
            return NoticeAnalysis(
                text.split()[0], "폼을 제출하세요", NOW + timedelta(days=2), "모레"
            )

    analyzer = Analyzer()
    notice_service = NoticeService(
        {"CALL"},
        analyzer=analyzer,
        repository=SqlAlchemyNoticeRepository(factory),
        on_change=daily_service.run_due,
    )
    source_ts = f"{int(NOW.timestamp()) - 86400}.000100"
    link = "https://forms.example.test/task"
    source = {
        "type": "message",
        "channel": "CALL",
        "channel_type": "channel",
        "ts": source_ts,
        "user": "UWRITER",
        "text": "첫과제 " + link,
    }

    def record_event(event):
        return notice_service.record_channel_message(
            event,
            workspace_id=WORKSPACE,
            source_permalink="https://workspace.slack.com/archives/CALL/p123",
        )

    def edit(text, seconds):
        return record_event(
            {
                "type": "message",
                "subtype": "message_changed",
                "channel": "CALL",
                "event_ts": f"{int(NOW.timestamp()) + seconds}.000100",
                "message": {"ts": source_ts, "text": text, "user": "UWRITER"},
            }
        )

    record_event(source)
    daily = get_daily(factory)[0]
    original_item = messenger.sent[0][1].items[0]
    click(daily_service, daily, "complete", original_item.id)
    click(daily_service, daily, "completed")
    with factory() as session:
        completed_at = session.get(ChecklistModel, original_item.id).completed_at
    edit("수정과제 " + link, 1)
    assert messenger.updated[-1][2].items[0].title == "수정과제"
    assert messenger.updated[-1][2].items[0].completed

    analyzer.fail = True
    edit("분석실패과제 " + link, 2)
    daily_service.run_due()
    assert messenger.updated[-1][2].items[0].title == "수정과제"

    edit("링크 없음", 3)
    assert messenger.updated[-1][2].items == ()
    with factory() as session:
        item = session.get(ChecklistModel, original_item.id)
        assert item.deleted_at is not None and item.completed_at == completed_at
        assert session.get(NoticeModel, item.notice_id).deleted_at is not None
    with pytest.raises(ChecklistActionError, match="삭제"):
        click(daily_service, daily, "undo", original_item.id)

    # 삭제했던 링크의 재분석 실패는 옛 분석으로 항목을 되살리지 않는다.
    edit("복구과제 " + link, 4)
    daily_service.run_due()
    assert messenger.updated[-1][2].items == ()
    analyzer.fail = False
    notice_service.retry_failed_notice(WORKSPACE, link)
    assert messenger.updated[-1][2].items[0].id == original_item.id
    assert messenger.updated[-1][2].items[0].title == "복구과제"
    assert messenger.updated[-1][2].items[0].completed

    edit("추가과제 " + link + " https://docs.example.test/added", 5)
    with factory() as session:
        assert len(session.execute(select(ChecklistModel)).scalars().all()) == 2
        assert session.get(ChecklistModel, original_item.id).completed_at == completed_at
    record_event(
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "CALL",
            "deleted_ts": source_ts,
            "event_ts": f"{int(NOW.timestamp()) + 6}.000100",
        }
    )
    assert messenger.updated[-1][2].items == ()
    with factory() as session:
        items = session.execute(select(ChecklistModel)).scalars().all()
        assert all(item.deleted_at is not None for item in items)
        assert session.get(ChecklistModel, original_item.id).completed_at == completed_at
    assert len(messenger.sent) == 1
    assert all(
        address[:2] == (daily.dm_channel_id, daily.message_ts) for address in messenger.updated
    )


def test_sends_immediately_once_and_reuses_message_across_days_and_restart(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    clock[0] = NOW - timedelta(seconds=1)
    service.run_due()
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1
    with factory() as session:
        assert len(session.execute(select(ChecklistModel)).scalars().all()) == 1
    clock[0] = NOW
    service.run_due()
    service.run_due()
    assert len(messenger.sent) == 1 and messenger.updated == []
    assert messenger.sent[0][1].message_date.isoformat() == "2026-09-16"
    clock[0] = NOW + timedelta(hours=15)  # 서울 다음 날 0시
    service.run_due()
    assert len(messenger.sent) == 1
    clock[0] = NOW + timedelta(days=1)
    service.run_due()
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1
    assert messenger.updated == []
    restarted = DailyChecklistService(
        SqlAlchemyChecklistRepository(factory),
        messenger,
        WORKSPACE,
        TARGETS,
        clock=lambda: clock[0],
    )
    restarted.run_due()
    add_notice(factory, title="다음 날 공지")
    restarted.run_due()
    daily = get_daily(factory)[0]
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1
    assert messenger.updated[-1][:2] == (daily.dm_channel_id, daily.message_ts)
    assert len(messenger.updated[-1][2].items) == 2


def test_empty_day_gets_dm_and_new_notice_updates_same_message(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    service.run_due()
    assert messenger.sent[0][1].items == ()
    daily = get_daily(factory)[0]
    add_notice(factory)
    service.run_due()
    assert len(messenger.sent) == 1
    assert messenger.updated[0][:2] == (daily.dm_channel_id, daily.message_ts)
    assert messenger.updated[0][2].pending_count == 1


@pytest.mark.parametrize("hour", [0, 8, 9, 23])
def test_first_connection_sends_at_any_seoul_hour(daily_system, hour):
    factory, _, messenger, clock, service = daily_system
    clock[0] = NOW + timedelta(hours=hour - 9)
    add_student(factory)
    service.run_due()
    service.run_due()
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1
    assert get_daily(factory)[0].message_date.isoformat() == "2026-09-16"


@pytest.mark.parametrize(("channel", "ts"), [("DUONE", None), (None, "1.1"), (None, None)])
def test_incomplete_sent_address_is_not_replaced_with_new_dm(daily_system, channel, ts):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    with factory() as session, session.begin():
        session.add(
            DailyChecklistMessageModel(
                student_id=student_id,
                message_date=NOW.date(),
                dm_channel_id=channel,
                message_ts=ts,
                status="sent",
            )
        )
    service.run_due()
    service.run_due()
    assert messenger.sent == [] and messenger.updated == []
    assert len(get_daily(factory)) == 1
    assert get_daily(factory)[0].status == "uncertain"


def test_presentation_change_updates_existing_dm_once_without_changing_completion(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    item = messenger.sent[0][1].items[0]
    click(service, daily, "complete", item.id)
    click(service, daily, "completed")
    legacy_content = asdict(messenger.updated[-1][2])
    legacy_content.pop("refreshed_at")
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_content, sort_keys=True, default=str).encode()
    ).hexdigest()
    with factory() as session, session.begin():
        session.get(DailyChecklistMessageModel, daily.id).content_hash = legacy_hash
        completed_at = session.get(ChecklistModel, item.id).completed_at
    previous_updates = len(messenger.updated)

    service.run_due()
    clock[0] += timedelta(minutes=5)
    service.run_due()

    assert len(messenger.sent) == 1 and len(messenger.updated) == previous_updates + 1
    assert messenger.updated[-1][:2] == (daily.dm_channel_id, daily.message_ts)
    assert messenger.updated[-1][2].items[0].completed
    assert get_daily(factory)[0].content_hash != legacy_hash
    with factory() as session:
        assert session.get(ChecklistModel, item.id).completed_at == completed_at


def test_filters_workspace_class_failed_deleted_and_expired_notices(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory, class_number=1)
    add_student(factory, user="UTHREE", class_number=3)
    add_student(factory, user="UOTHER", workspace="TOTHER")
    add_notice(factory, title="전체")
    add_notice(factory, channel_id="CCLASS3", title="3반")
    add_notice(factory, channel_id="CUNKNOWN")
    add_notice(factory, workspace_id="TOTHER")
    add_notice(factory, processing_status="processing_failed", title=None, summary=None)
    add_notice(factory, processing_status="ai_disabled")
    add_notice(factory, deleted_at=NOW)
    add_notice(factory, deadline_at=NOW)
    service.run_due()
    results = {user: [item.title for item in board.items] for user, board in messenger.sent}
    assert results["UONE"] == ["전체"]
    assert set(results["UTHREE"]) == {"전체", "3반"}
    assert "UOTHER" not in results


def test_late_signup_and_retry_success_are_picked_up_without_duplicate_assignment(daily_system):
    factory, _, messenger, clock, service = daily_system
    notice_id = add_notice(factory, processing_status="processing_failed", title=None, summary=None)
    clock[0] = NOW + timedelta(hours=4)
    add_student(factory)
    service.run_due()
    assert messenger.sent[0][1].pending_count == 0
    with factory() as session, session.begin():
        notice = session.get(NoticeModel, notice_id)
        notice.processing_status = "processed"
        notice.title, notice.summary = "복구 공지", "제출하세요"
    service.run_due()
    service.run_due()
    assert len(messenger.sent) == 1 and len(messenger.updated) == 1
    with factory() as session:
        assert len(session.execute(select(ChecklistModel)).scalars().all()) == 1


def test_complete_duplicate_click_view_and_undo_use_same_dm(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    item_id = messenger.sent[0][1].items[0].id
    click(service, daily, "complete", item_id)
    with factory() as session:
        completed_at = session.get(ChecklistModel, item_id).completed_at
    clock[0] += timedelta(minutes=1)
    click(service, daily, "complete", item_id)
    with factory() as session:
        assert session.get(ChecklistModel, item_id).completed_at == completed_at
    assert messenger.updated[-1][2].pending_count == 0
    click(service, daily, "completed")
    assert messenger.updated[-1][2].items[0].completed
    click(service, daily, "undo", item_id)
    click(service, daily, "pending")
    assert messenger.updated[-1][2].pending_count == 1
    assert not messenger.updated[-1][2].items[0].completed
    assert len(messenger.sent) == 1


def test_second_survey_is_assigned_while_completed_filter_stays_on(daily_system):
    factory, _, messenger, _, daily_service = daily_system
    student_id = add_student(factory)

    class SurveyAnalyzer:
        def analyze(self, text, url, posted_at):
            spring = "스프링" in text
            return NoticeAnalysis(
                "스프링 개발 서베이" if spring else "모델 개발 서베이",
                "스프링 서베이에 참여하세요" if spring else "모델 서베이에 참여하세요",
                NOW + timedelta(days=10 if spring else 11),
                "9/26(토)" if spring else "9/27(일)",
            )

    service = NoticeService(
        {"CALL"},
        analyzer=SurveyAnalyzer(),
        repository=SqlAlchemyNoticeRepository(factory),
        on_change=daily_service.run_due,
    )

    def post(course, path, seconds, deadline):
        link = f"https://forms.example.test/{path}"
        return service.record_channel_message(
            {
                "type": "message",
                "channel": "CALL",
                "channel_type": "channel",
                "ts": f"{int(NOW.timestamp()) + seconds}.000100",
                "user": "UWRITER",
                "text": f"{course} 교과목 종료 서베이 <{link}>에 참여해주세요!\n"
                f"※ 마감일 : ~{deadline}",
            },
            workspace_id=WORKSPACE,
            source_permalink=f"https://workspace.slack.com/archives/CALL/p{path}",
        )

    post("모델 개발", "model", 0, "9/27(일)")
    daily = get_daily(factory)[0]
    first_item = messenger.sent[0][1].items[0]
    click(daily_service, daily, "complete", first_item.id)
    click(daily_service, daily, "completed")

    post("스프링 개발", "spring", 1, "9/26(토)")

    completed_board = messenger.updated[-1][2]
    assert completed_board.show_completed
    assert (completed_board.pending_count, completed_board.completed_count) == (1, 1)
    assert [item.title for item in completed_board.items] == ["모델 개발 서베이"]
    assert completed_board.items[0].completed
    with factory() as session:
        assert len(session.scalars(select(NoticeModel)).all()) == 2
        assigned = session.scalars(
            select(ChecklistModel).where(ChecklistModel.student_id == student_id)
        ).all()
        assert len(assigned) == 2 and sum(item.completed_at is not None for item in assigned) == 1

    click(daily_service, daily, "refresh")
    assert get_daily(factory)[0].show_completed
    click(daily_service, daily, "pending")
    pending_board = messenger.updated[-1][2]
    assert not pending_board.show_completed
    assert [item.title for item in pending_board.items] == ["스프링 개발 서베이"]
    assert pending_board.items[0].original_url == "https://forms.example.test/spring"
    assert pending_board.items[0].deadline_at < first_item.deadline_at
    assert not pending_board.items[0].completed
    assert len(messenger.sent) == 1
    assert messenger.updated[-1][:2] == (daily.dm_channel_id, daily.message_ts)


@pytest.mark.parametrize(
    "case",
    [
        "other_user",
        "other_workspace",
        "expired",
        "deleted",
        "withdrawn",
        "wrong_message",
        "wrong_item",
    ],
)
def test_unauthorized_and_stale_buttons_cannot_change_state(daily_system, case):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_student(factory, user="UOTHER")
    notice_id = add_notice(factory)
    service.run_due()
    daily = next(row for row in get_daily(factory) if row.student_id == student_id)
    board = next(board for user, board in messenger.sent if user == "UONE")
    item_id = board.items[0].id
    kwargs = {}
    if case == "other_user":
        kwargs["user"] = "UOTHER"
    elif case == "other_workspace":
        kwargs["workspace"] = "TOTHER"
    elif case == "wrong_message":
        daily.message_ts = "999.999"
    elif case == "wrong_item":
        item_id = uuid4()
    else:
        with factory() as session, session.begin():
            if case == "expired":
                session.get(NoticeModel, notice_id).deadline_at = NOW
            elif case == "deleted":
                session.get(NoticeModel, notice_id).deleted_at = NOW
            elif case == "withdrawn":
                session.execute(delete(StudentModel).where(StudentModel.id == student_id))
    with pytest.raises(ChecklistActionError):
        click(service, daily, "complete", item_id, **kwargs)
    with factory() as session:
        checklist = session.get(ChecklistModel, item_id)
        assert checklist is None or checklist.completed_at is None


@pytest.mark.parametrize("old_class,new_class", [(3, 1), (2, 4)])
def test_live_profile_change_keeps_dm_and_completion_and_reassigns_class(
    daily_system, old_class, new_class
):
    from seulseul.users.repository import SqlAlchemyStudentRepository
    from seulseul.users.service import StudentService

    factory, repository, messenger, clock, _ = daily_system
    student_id = add_student(factory, class_number=old_class)
    targets = {"CALL": None, "COLD": old_class, "CNEW": new_class}
    service = DailyChecklistService(
        repository, messenger, WORKSPACE, targets, clock=lambda: clock[0]
    )
    add_notice(factory, channel_id="COLD", title="이전 반")
    add_notice(factory, channel_id="CNEW", title="새 반")
    add_notice(factory, title="전체 공지")
    service.run_due()
    daily = get_daily(factory)[0]
    old_item = next(item for item in messenger.sent[0][1].items if item.title == "이전 반")
    click(service, daily, "complete", old_item.id)
    users = StudentService(
        SqlAlchemyStudentRepository(factory), profile_update=service.profile_update
    )
    assert users.sync_profile(WORKSPACE, "UONE", lambda user: f"4기_광주_{new_class}반_가상학생")
    service.run_due()
    assert len(messenger.sent) == 1
    assert {i.title for i in messenger.updated[-1][2].items} == {"새 반", "전체 공지"}
    current = get_daily(factory)[0]
    assert (current.id, current.dm_channel_id, current.message_ts) == (
        daily.id,
        daily.dm_channel_id,
        daily.message_ts,
    )
    with factory() as session:
        assert session.get(StudentModel, student_id).class_number == new_class
        assert session.get(ChecklistModel, old_item.id).completed_at is not None


def test_expiry_and_class_change_refresh_visibility_without_resetting_completion(daily_system):
    factory, _, messenger, clock, service = daily_system
    student_id = add_student(factory)
    add_notice(factory, channel_id="CCLASS3")
    add_notice(factory, deadline_at=NOW + timedelta(minutes=1))
    service.run_due()
    daily = get_daily(factory)[0]
    board = messenger.sent[0][1]
    item = next(item for item in board.items if item.deadline_at > NOW + timedelta(days=1))
    click(service, daily, "complete", item.id)
    clock[0] += timedelta(minutes=2)
    with factory() as session, session.begin():
        session.get(StudentModel, student_id).class_number = 1
    service.run_due()
    assert messenger.updated[-1][2].pending_count == 0
    assert messenger.updated[-1][2].completed_count == 0
    with factory() as session:
        assert session.get(ChecklistModel, item.id).completed_at is not None


def test_pagination_and_filters_do_not_create_extra_messages(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    for _ in range(6):
        add_notice(factory)
    service.run_due()
    assert len(messenger.sent[0][1].items) == 5
    daily = get_daily(factory)[0]
    click(service, daily, "next")
    assert len(messenger.updated[-1][2].items) == 1
    assert messenger.updated[-1][2].page == 1
    click(service, daily, "previous")
    assert messenger.updated[-1][2].page == 0
    assert len(messenger.sent) == 1


def test_delivery_lease_blocks_parallel_send_and_unknown_result_blocks_resend(daily_system):
    factory, repository, messenger, clock, service = daily_system
    add_student(factory)
    recipient = repository.recipients(WORKSPACE)[0]
    claim = repository.prepare_delivery(recipient, TARGETS, NOW)
    assert claim is not None
    assert repository.prepare_delivery(recipient, TARGETS, NOW) is None
    clock[0] += timedelta(minutes=4)
    service.run_due()
    assert messenger.sent == []
    assert get_daily(factory)[0].status == "uncertain"


def test_missing_explicit_delivery_does_not_create_replacement(daily_system):
    factory, repository, _, _, _ = daily_system
    add_student(factory)
    recipient = repository.recipients(WORKSPACE)[0]
    assert repository.prepare_delivery(recipient, TARGETS, NOW, daily_id=uuid4()) is None
    assert get_daily(factory) == []


def test_known_first_send_failure_retries_same_record(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    messenger.error = ChecklistDeliveryError("ratelimited", retry_after=60)
    service.run_due()
    assert get_daily(factory)[0].status == "retry"
    messenger.error = None
    clock[0] += timedelta(seconds=30)
    service.run_due()
    assert messenger.sent == []
    clock[0] += timedelta(seconds=30)
    service.run_due()
    assert len(messenger.sent) == 1
    assert len(get_daily(factory)) == 1


def test_uncertain_first_send_does_not_retry_even_after_date_change(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    messenger.error = ChecklistDeliveryError("connection_lost", uncertain=True)
    service.run_due()
    messenger.error = None
    clock[0] += timedelta(days=2)
    service.run_due()
    assert messenger.sent == [] and messenger.updated == []
    assert len(get_daily(factory)) == 1 and get_daily(factory)[0].status == "uncertain"


def test_update_failure_keeps_message_address_and_retries_edit(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    service.run_due()
    original = get_daily(factory)[0]
    add_notice(factory)
    messenger.error = ChecklistDeliveryError("connection_lost", uncertain=True)
    service.run_due()
    assert get_daily(factory)[0].status == "retry"
    messenger.error = None
    clock[0] += timedelta(minutes=2)
    service.run_due()
    assert len(messenger.sent) == 1
    assert messenger.updated[-1][:2] == (original.dm_channel_id, original.message_ts)


def test_withdraw_cascades_daily_records_and_checklists(daily_system):
    factory, _, messenger, clock, service = daily_system
    student_id = add_student(factory)
    add_notice(factory)
    service.run_due()
    with factory() as session, session.begin():
        session.execute(delete(StudentModel).where(StudentModel.id == student_id))
    clock[0] += timedelta(days=1)
    service.run_due()
    assert len(messenger.sent) == 1 and get_daily(factory) == []
    with factory() as session:
        assert session.execute(select(ChecklistModel)).first() is None
        assert session.execute(select(NoticeModel)).first() is not None


@pytest.mark.parametrize("payload", ["bad-json", "null", "[]", "{}", '{"daily":"bad-id"}'])
def test_invalid_button_payload_is_rejected(daily_system, payload):
    _, _, _, _, service = daily_system
    with pytest.raises(ChecklistActionError, match="버튼 정보"):
        service.handle_action(WORKSPACE, "UONE", "D1", "1.1", "complete", payload)


def test_original_buttons_work_after_midnight_and_preserve_completion_next_day(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    clock[0] = NOW + timedelta(hours=16)  # 다음 날 서울 오전 1시
    item_id = messenger.sent[0][1].items[0].id
    click(service, daily, "complete", item_id)
    click(service, daily, "completed")
    assert messenger.updated[-1][2].items[0].completed
    clock[0] = NOW + timedelta(days=1)
    service.run_due()
    assert get_daily(factory)[0].show_completed
    click(service, daily, "undo", item_id)
    click(service, daily, "pending")
    assert not messenger.updated[-1][2].items[0].completed
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1


def test_button_rejects_timestamp_with_different_string_representation(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    add_notice(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    item_id = messenger.sent[0][1].items[0].id

    with pytest.raises(ChecklistActionError):
        service.handle_action(
            WORKSPACE,
            "UONE",
            daily.dm_channel_id,
            f"{float(daily.message_ts):.6f}",
            "complete",
            json.dumps({"daily": str(daily.id), "item": str(item_id)}),
        )

    with factory() as session:
        assert session.get(ChecklistModel, item_id).completed_at is None
    assert messenger.updated == []


def test_legacy_daily_records_reuse_first_message_and_reject_other_buttons(daily_system):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    with factory() as session, session.begin():
        oldest = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=(NOW - timedelta(days=3)).date(),
            dm_channel_id="DUONE",
            message_ts="1.1",
            status="sent",
            show_completed=True,
        )
        newer = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=NOW.date(),
            dm_channel_id="DUONE",
            message_ts="2.2",
            status="sent",
        )
        failed = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=(NOW - timedelta(days=4)).date(),
            status="retry",
        )
        session.add_all([oldest, newer, failed])
    service.run_due()
    assert messenger.sent == [] and len(get_daily(factory)) == 3
    assert messenger.updated[-1][:2] == ("DUONE", "1.1")
    assert messenger.updated[-1][2].show_completed
    with pytest.raises(ChecklistActionError):
        click(service, newer, "pending")
    assert len(messenger.updated) == 1
    assert click(service, oldest, "pending")


def test_button_cannot_change_completion_from_non_current_message(daily_system, caplog):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_notice(factory)
    with factory() as session, session.begin():
        oldest = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=(NOW - timedelta(days=3)).date(),
            dm_channel_id="DUONE",
            message_ts="1.1",
            status="sent",
        )
        current = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=NOW.date(),
            dm_channel_id="DUONE",
            message_ts="2.2",
            status="sent",
        )
        session.add_all([oldest, current])

    service.run_due()
    with factory() as session:
        item = session.scalar(select(ChecklistModel).where(ChecklistModel.student_id == student_id))

    caplog.set_level(logging.WARNING)
    with pytest.raises(ChecklistActionError):
        service.handle_action(
            WORKSPACE,
            "UONE",
            current.dm_channel_id,
            current.message_ts,
            "complete",
            json.dumps({"daily": str(current.id), "item": str(item.id)}),
        )

    with factory() as session:
        assert session.get(ChecklistModel, item.id).completed_at is None
    assert len(messenger.updated) == 1
    assert messenger.updated[-1][:2] == (oldest.dm_channel_id, oldest.message_ts)
    validation = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if json.loads(record.getMessage())["event"] == "checklist_message_validation"
    )
    assert validation["result"] == "rejected"
    assert validation["reason"] == "delivery_not_current"


@pytest.mark.parametrize("reference", ["missing", "other_student", "other_message"])
@pytest.mark.parametrize(
    "operation", ["complete", "undo", "pending", "completed", "previous", "next", "refresh"]
)
def test_button_rejects_wrong_record_id_even_with_matching_message_address(
    daily_system, reference, operation
):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_notice(factory)
    with factory() as session, session.begin():
        daily = DailyChecklistMessageModel(
            student_id=student_id,
            message_date=NOW.date(),
            dm_channel_id="DUONE",
            message_ts="2.2",
            status="sent",
        )
        session.add(daily)

    referenced_id = uuid4()
    if reference != "missing":
        owner_id = (
            add_student(factory, user="UOTHER") if reference == "other_student" else student_id
        )
        with factory() as session, session.begin():
            session.add(
                DailyChecklistMessageModel(
                    id=referenced_id,
                    student_id=owner_id,
                    message_date=(NOW + timedelta(days=1)).date(),
                    dm_channel_id="DOTHER",
                    message_ts="3.3",
                    status="sent",
                )
            )

    service.run_due()
    messenger.updated.clear()
    with factory() as session:
        item = session.scalar(select(ChecklistModel).where(ChecklistModel.student_id == student_id))

    with pytest.raises(ChecklistActionError):
        service.handle_action(
            WORKSPACE,
            "UONE",
            daily.dm_channel_id,
            daily.message_ts,
            operation,
            json.dumps({"daily": str(referenced_id), "item": str(item.id)}),
        )

    with factory() as session:
        assert session.get(ChecklistModel, item.id).completed_at is None
        stored = session.get(DailyChecklistMessageModel, daily.id)
        assert not stored.show_completed and stored.page == 0
    assert messenger.updated == []


@pytest.mark.parametrize(
    "case,reason",
    [
        ("channel", "channel_mismatch"),
        ("timestamp", "timestamp_mismatch"),
        ("missing", "delivery_not_found"),
        ("owner", "delivery_owner_mismatch"),
    ],
)
def test_button_diagnostics_identify_exact_message_mismatch(daily_system, caplog, case, reason):
    from seulseul.slack.handlers import create_checklist_action_handler

    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    other_id = add_student(factory, user="UOTHER")
    add_notice(factory)
    service.run_due()
    daily = next(row for row in get_daily(factory) if row.student_id == student_id)
    other = next(row for row in get_daily(factory) if row.student_id == other_id)
    daily_id = daily.id
    channel, ts = daily.dm_channel_id, daily.message_ts
    if case == "channel":
        channel = "DWRONG"
    elif case == "timestamp":
        ts = "999.999"
    elif case == "missing":
        with factory() as session, session.begin():
            session.execute(
                delete(DailyChecklistMessageModel).where(DailyChecklistMessageModel.id == daily.id)
            )
    else:
        daily_id, channel, ts = other.id, other.dm_channel_id, other.message_ts

    caplog.set_level(logging.INFO)
    caplog.clear()
    replies = []
    create_checklist_action_handler(service)(
        lambda: None,
        lambda text, **kwargs: replies.append(text),
        {
            "team": {"id": WORKSPACE},
            "user": {"id": "UONE"},
            "container": {"channel_id": channel, "message_ts": ts},
            "actions": [
                {"action_id": "checklist_refresh", "value": json.dumps({"daily": str(daily_id)})}
            ],
        },
        logging.getLogger("test"),
    )
    assert replies == ["현재 연결된 본인의 체크리스트에서만 변경할 수 있습니다."]
    logs = [json.loads(record.getMessage()) for record in caplog.records]
    assert len({entry["trace_id"] for entry in logs}) == 1
    validation = next(entry for entry in logs if entry["event"] == "checklist_message_validation")
    assert validation["result"] == "rejected"
    assert validation["reason"] == reason
    assert validation["received_message_ts"] == ts
    assert validation["matched_daily_id"] is None
    if case in {"channel", "timestamp"}:
        assert validation["stored_message_ts"] == daily.message_ts
        assert validation["channel_matches"] == (case != "channel")
        assert validation["timestamp_matches"] == (case != "timestamp")
    if case == "owner":
        assert not validation["payload_record_owned"]
        assert validation["stored_channel_ref"] is None
    assert len(validation["student_records_sample"]) <= 3
    assert WORKSPACE not in caplog.text and "UONE" not in caplog.text
    assert other.dm_channel_id not in caplog.text
    assert messenger.updated == []
    with factory() as session:
        assert len(session.scalars(select(StudentModel)).all()) == 2


def test_button_diagnostics_report_exact_match_and_success(daily_system, caplog):
    factory, _, _, _, service = daily_system
    add_student(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    caplog.set_level(logging.INFO)
    caplog.clear()
    assert service.handle_action(
        WORKSPACE,
        "UONE",
        daily.dm_channel_id,
        daily.message_ts,
        "completed",
        json.dumps({"daily": str(daily.id)}),
        trace_id="test-trace",
    )
    logs = [json.loads(record.getMessage()) for record in caplog.records]
    validation = next(entry for entry in logs if entry["event"] == "checklist_message_validation")
    assert validation["result"] == "payload_match"
    assert validation["matched_daily_id"] == str(daily.id)
    assert logs[-1]["event"] == "checklist_action_result"
    assert logs[-1]["saved"] and logs[-1]["dm_synchronized"]
    assert all(entry["trace_id"] == "test-trace" for entry in logs)


def test_first_delivery_logs_committed_address_only_once(daily_system, caplog):
    factory, _, _, _, service = daily_system
    add_student(factory)
    caplog.set_level(logging.INFO)
    service.run_due()
    service.run_due()
    logs = [json.loads(record.getMessage()) for record in caplog.records]
    assert len(logs) == 1
    daily = get_daily(factory)[0]
    assert logs[0]["event"] == "checklist_delivery_saved"
    assert logs[0]["daily_id"] == str(daily.id)
    assert logs[0]["message_ts"] == daily.message_ts
    assert logs[0]["rows_updated"] == 1


def test_delivery_logs_lost_claim_without_saying_address_was_saved(daily_system, caplog):
    from dataclasses import replace

    factory, repository, _, _, _ = daily_system
    add_student(factory)
    claim = repository.prepare_delivery(repository.recipients(WORKSPACE)[0], TARGETS, NOW)
    assert claim is not None
    caplog.set_level(logging.INFO)
    repository.finish_delivery(replace(claim, token=uuid4()), "hash", "DUONE", "100.1")
    entry = json.loads(caplog.records[-1].getMessage())
    assert entry["event"] == "checklist_delivery_save_conflict"
    assert entry["rows_updated"] == 0
    assert get_daily(factory)[0].message_ts is None


def test_invalid_payload_and_timestamp_are_not_echoed_in_diagnostics(daily_system, caplog):
    factory, _, _, _, service = daily_system
    add_student(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    caplog.set_level(logging.INFO)
    with pytest.raises(ChecklistActionError):
        service.handle_action(
            WORKSPACE,
            "UONE",
            daily.dm_channel_id,
            "secret-timestamp",
            "refresh",
            json.dumps({"daily": str(daily.id)}),
        )
    with pytest.raises(ChecklistActionError):
        service.handle_action(
            WORKSPACE,
            "UONE",
            daily.dm_channel_id,
            daily.message_ts,
            "refresh",
            "secret-payload",
        )
    assert "secret-timestamp" not in caplog.text
    assert "secret-payload" not in caplog.text
    logs = [json.loads(record.getMessage()) for record in caplog.records]
    validation = next(entry for entry in logs if entry["event"] == "checklist_message_validation")
    assert validation["received_message_ts"] == "invalid"
    assert logs[-1]["event"] == "checklist_action_invalid_value"


def test_legacy_uncertain_record_blocks_new_send_instead_of_using_newer_pending(daily_system):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    with factory() as session, session.begin():
        session.add_all(
            [
                DailyChecklistMessageModel(
                    student_id=student_id,
                    message_date=(NOW - timedelta(days=1)).date(),
                    status="uncertain",
                ),
                DailyChecklistMessageModel(student_id=student_id, message_date=NOW.date()),
            ]
        )
    service.run_due()
    assert messenger.sent == [] and messenger.updated == []
    assert len(get_daily(factory)) == 2


def test_deleted_slack_message_does_not_trigger_replacement_send(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    service.run_due()
    original = get_daily(factory)[0]
    add_notice(factory)
    messenger.error = ChecklistDeliveryError("message_not_found")
    service.run_due()
    clock[0] += timedelta(days=1)
    service.run_due()
    assert len(messenger.sent) == 1 and len(get_daily(factory)) == 1
    assert get_daily(factory)[0].message_ts == original.message_ts
    assert get_daily(factory)[0].last_error == "message_not_found"


def test_reenrollment_starts_new_connection_but_old_message_cannot_change_it(daily_system):
    factory, _, messenger, _, service = daily_system
    student_id = add_student(factory)
    add_notice(factory)
    service.run_due()
    original = get_daily(factory)[0]
    with factory() as session, session.begin():
        session.execute(delete(StudentModel).where(StudentModel.id == student_id))
    add_student(factory)
    service.run_due()
    assert len(messenger.sent) == 2 and len(get_daily(factory)) == 1
    assert get_daily(factory)[0].id != original.id
    with pytest.raises(ChecklistActionError):
        click(service, original, "refresh")


def test_notice_analysis_changes_preserve_student_completion(daily_system):
    factory, _, messenger, _, service = daily_system
    add_student(factory)
    notice_id = add_notice(factory)
    service.run_due()
    item_id = messenger.sent[0][1].items[0].id
    daily = get_daily(factory)[0]
    click(service, daily, "complete", item_id)
    click(service, daily, "completed")
    with factory() as session, session.begin():
        session.get(NoticeModel, notice_id).title = "수정된 제목"
    service.run_due()
    assert messenger.updated[-1][2].items[0].title == "수정된 제목"
    assert messenger.updated[-1][2].items[0].completed
