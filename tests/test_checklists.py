"""체크리스트 ORM 모델의 관계와 상태 컬럼을 확인한다."""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import UniqueConstraint, create_engine, delete, event, select
from sqlalchemy.orm import configure_mappers, sessionmaker

from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistModel,
    DailyChecklistMessageModel,
)
from seulseul.checklists.repository import SqlAlchemyChecklistRepository
from seulseul.checklists.service import DailyChecklistService
from seulseul.database import Base
from seulseul.notices.model import NoticeModel
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


def test_assigns_before_nine_and_sends_one_dm_per_seoul_day(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    clock[0] = NOW - timedelta(seconds=1)
    service.run_due()
    assert messenger.sent == [] and get_daily(factory) == []
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
    assert len(messenger.sent) == 2 and len(get_daily(factory)) == 2


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


@pytest.mark.parametrize(
    "case",
    [
        "other_user",
        "other_workspace",
        "old_day",
        "expired",
        "deleted",
        "withdrawn",
        "wrong_message",
        "wrong_item",
    ],
)
def test_unauthorized_and_stale_buttons_cannot_change_state(daily_system, case):
    factory, _, messenger, clock, service = daily_system
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
    elif case == "old_day":
        clock[0] += timedelta(days=1)
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
    claim = repository.prepare_delivery(recipient, TARGETS, NOW.date(), NOW)
    assert claim is not None
    assert repository.prepare_delivery(recipient, TARGETS, NOW.date(), NOW) is None
    clock[0] += timedelta(minutes=4)
    service.run_due()
    assert messenger.sent == []
    assert get_daily(factory)[0].status == "uncertain"


def test_known_failure_retries_but_uncertain_first_send_does_not(daily_system):
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
    clock[0] += timedelta(days=1)
    messenger.error = ChecklistDeliveryError("connection_lost", uncertain=True)
    service.run_due()
    messenger.error = None
    clock[0] += timedelta(minutes=10)
    service.run_due()
    assert len(messenger.sent) == 1
    assert any(row.status == "uncertain" for row in get_daily(factory))


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


def test_buttons_before_nine_do_not_change_yesterdays_items(daily_system):
    factory, _, messenger, clock, service = daily_system
    add_student(factory)
    add_notice(factory)
    service.run_due()
    daily = get_daily(factory)[0]
    clock[0] = NOW + timedelta(hours=16)  # 다음 날 서울 오전 1시
    with pytest.raises(ChecklistActionError, match="오전 9시"):
        click(service, daily, "complete", messenger.sent[0][1].items[0].id)


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
