"""체크리스트 배정·개인 상태와 최초 DM의 전송·갱신 기록을 저장한다."""

import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, select, update
from sqlalchemy.orm import Session

from seulseul.checklists.diagnostics import identifier_ref, log_event, timestamp_for_log
from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistItem,
    ChecklistModel,
    ChecklistRecipient,
    DailyChecklistBoard,
    DailyChecklistMessageModel,
    DeliveryClaim,
)
from seulseul.notices.model import NoticeModel
from seulseul.users.model import StudentModel

PAGE_SIZE = 5
LEASE_SECONDS = 180
logger = logging.getLogger(__name__)


def _same_message_timestamp(expected: str | None, received: str) -> bool:
    """Slack timestamp의 소수점 자릿수 차이를 허용해 같은 메시지인지 확인한다."""
    if expected is None or expected == received:
        return expected == received
    try:
        return Decimal(expected) == Decimal(received)
    except (InvalidOperation, ValueError):
        return False


def set_notice_checklists_deleted(
    session: Session, notice_ids: Collection[UUID], deleted_at: datetime | None
) -> None:
    """원본 동기화 트랜잭션 안에서 삭제 상태만 바꾸고 완료 기록은 보존한다."""
    if notice_ids:
        session.execute(
            update(ChecklistModel)
            .where(ChecklistModel.notice_id.in_(notice_ids))
            .values(deleted_at=deleted_at)
        )


def _aware(value: datetime) -> datetime:
    # PostgreSQL은 aware datetime을 반환한다. SQLite 격리 테스트의 UTC 값도 지원한다.
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class SqlAlchemyChecklistRepository:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def recipients(self, workspace_id: str) -> list[ChecklistRecipient]:
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(StudentModel).where(
                        StudentModel.workspace_id == workspace_id,
                        StudentModel.campus == "광주",
                        StudentModel.class_number.between(1, 4),
                    )
                )
                .scalars()
                .all()
            )
            return [
                ChecklistRecipient(row.id, row.workspace_id, row.slack_user_id, row.class_number)
                for row in rows
            ]

    def recipient(self, workspace_id: str, user_id: str) -> ChecklistRecipient | None:
        return next((row for row in self.recipients(workspace_id) if row.user_id == user_id), None)

    def reset_messages(self, workspace_id: str, user_id: str, *, finished: bool) -> None:
        """삭제 도중 중단돼도 자동 재발송하지 않는다. 완료 기록은 변경하지 않는다."""
        student_ids = select(StudentModel.id).where(
            StudentModel.workspace_id == workspace_id, StudentModel.slack_user_id == user_id
        )
        with self._session_factory() as session, session.begin():
            condition = DailyChecklistMessageModel.student_id.in_(student_ids)
            if finished:
                session.execute(delete(DailyChecklistMessageModel).where(condition))
            else:
                result = session.execute(
                    update(DailyChecklistMessageModel)
                    .where(condition)
                    .values(
                        status="uncertain",
                        last_error="dm_cleanup_pending",
                        lease_token=None,
                        lease_until=None,
                    )
                )
                student_id = session.scalar(student_ids)
                if not result.rowcount and student_id is not None:
                    session.add(
                        DailyChecklistMessageModel(
                            student_id=student_id,
                            message_date=datetime.now(ZoneInfo("Asia/Seoul")).date(),
                            status="uncertain",
                            last_error="dm_cleanup_pending",
                        )
                    )

    def prepare_delivery(
        self,
        recipient: ChecklistRecipient,
        channels: Collection[str],
        now: datetime,
        *,
        class_channels: Collection[str] = (),
        daily_id: UUID | None = None,
    ) -> DeliveryClaim | None:
        """짧은 트랜잭션에서 배정·발송권 획득. Slack 호출 전에 커밋한다."""
        with self._session_factory() as session, session.begin():
            student = session.execute(
                select(StudentModel)
                .where(
                    StudentModel.id == recipient.id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if student is None or student.class_number != recipient.class_number:
                return None

            eligible = self._eligible(channels, now)
            notices = (
                session.execute(
                    select(NoticeModel)
                    .where(
                        NoticeModel.workspace_id == recipient.workspace_id,
                        *eligible,
                    )
                    .with_for_update(read=True)
                )
                .scalars()
                .all()
            )
            assigned = {
                item.canonical_url: item
                for item in session.execute(
                    select(ChecklistModel).where(
                        ChecklistModel.student_id == recipient.id,
                    )
                ).scalars()
            }
            # 같은 학생에게 유효한 원본 중 반 공지 우선, 그다음 최신 게시 원본.
            selected = {}
            for notice in sorted(
                notices,
                key=lambda n: (
                    n.channel_id in class_channels,
                    _aware(n.posted_at),
                    n.channel_id,
                    n.message_ts,
                    str(n.id),
                ),
                reverse=True,
            ):
                if notice.canonical_url not in assigned and notice.processing_status != "processed":
                    continue
                selected.setdefault(notice.canonical_url, notice)
            for canonical_url, notice in selected.items():
                item = assigned.get(canonical_url)
                if item is None:
                    session.add(
                        ChecklistModel(
                            student_id=recipient.id,
                            notice_id=notice.id,
                            canonical_url=canonical_url,
                        )
                    )
                else:
                    # 체크리스트 ID와 완료 기록은 그대로 두고 표시 원본만 전환한다.
                    item.notice_id = notice.id
                    item.deleted_at = None
            session.flush()

            daily = self._message_for_student(session, recipient.id, daily_id=daily_id)
            if daily is None:
                if daily_id is not None:
                    # 버튼 처리 중 대상 기록이 사라져도 새 DM 발송으로 바꾸지 않는다.
                    return None
                daily = DailyChecklistMessageModel(
                    student_id=recipient.id,
                    message_date=now.astimezone(ZoneInfo("Asia/Seoul")).date(),
                )
                session.add(daily)
                session.flush()
            if daily.status == "uncertain":
                return None
            if bool(daily.dm_channel_id) != bool(daily.message_ts) or (
                daily.status == "sent" and not daily.message_ts
            ):
                daily.status = "uncertain"
                daily.last_error = "invalid_delivery_address"
                logger.warning("DM 주소 불완전: delivery=%s 운영자 확인 필요", daily.id)
                return None
            if daily.lease_until is not None and _aware(daily.lease_until) > now:
                return None
            if daily.status == "sending" and daily.message_ts is None:
                # 첫 발송이 응답 저장 전에 중단됐다면 재발송으로 중복을 만들지 않는다.
                daily.status = "uncertain"
                daily.last_error = "delivery_result_unknown"
                daily.lease_token = None
                daily.lease_until = None
                logger.warning("첫 DM 발송 결과 불명확: delivery=%s 운영자 확인 필요", daily.id)
                return None
            if daily.next_attempt_at is not None and _aware(daily.next_attempt_at) > now:
                return None

            rows = session.execute(
                select(ChecklistModel, NoticeModel)
                .join(
                    NoticeModel,
                    NoticeModel.id == ChecklistModel.notice_id,
                )
                .where(
                    ChecklistModel.student_id == recipient.id,
                    ChecklistModel.deleted_at.is_(None),
                    NoticeModel.workspace_id == recipient.workspace_id,
                    *eligible,
                )
                .order_by(NoticeModel.deadline_at, ChecklistModel.id)
            ).all()
            items = [
                ChecklistItem(
                    checklist.id,
                    notice.title,
                    notice.summary,
                    _aware(notice.deadline_at),
                    notice.original_url,
                    notice.source_permalink,
                    checklist.completed_at is not None,
                )
                for checklist, notice in rows
            ]
            pending_count = sum(not item.completed for item in items)
            completed_count = len(items) - pending_count
            visible = [item for item in items if item.completed == daily.show_completed]
            pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
            daily.page = min(daily.page, pages - 1)
            board = DailyChecklistBoard(
                daily.id,
                daily.message_date,
                tuple(visible[daily.page * PAGE_SIZE : (daily.page + 1) * PAGE_SIZE]),
                pending_count,
                completed_count,
                daily.show_completed,
                daily.page,
                pages,
                now,
            )
            token = uuid4()
            daily.status = "sending"
            daily.lease_token = token
            daily.lease_until = now + timedelta(seconds=LEASE_SECONDS)
            return DeliveryClaim(
                token,
                recipient.workspace_id,
                recipient.user_id,
                daily.dm_channel_id,
                daily.message_ts,
                daily.content_hash,
                board,
            )

    @staticmethod
    def _message_for_student(
        session: Session, student_id: UUID, *, daily_id: UUID | None = None
    ) -> DailyChecklistMessageModel | None:
        """학생 잠금 아래 최초 또는 지정된 체크리스트 메시지를 선택한다."""
        model = DailyChecklistMessageModel
        statement = select(model).where(model.student_id == student_id)
        if daily_id is None:
            statement = statement.order_by(
                case(
                    ((model.dm_channel_id.is_not(None)) & (model.message_ts.is_not(None)), 0),
                    (model.status.in_(("sending", "uncertain")), 1),
                    else_=2,
                ),
                model.message_date,
                model.id,
            )
        else:
            statement = statement.where(model.id == daily_id)
        return session.execute(statement.limit(1).with_for_update()).scalar_one_or_none()

    @staticmethod
    def _message_by_address(
        session: Session, student_id: UUID, channel_id: str, message_ts: str
    ) -> DailyChecklistMessageModel | None:
        """payload ID가 오래돼도 학생에게 저장된 동일 Slack 메시지를 찾는다."""
        model = DailyChecklistMessageModel
        rows = session.execute(
            select(model)
            .where(
                model.student_id == student_id,
                model.dm_channel_id == channel_id,
                model.message_ts.is_not(None),
            )
            .with_for_update()
        ).scalars()
        return next(
            (row for row in rows if _same_message_timestamp(row.message_ts, message_ts)),
            None,
        )

    @staticmethod
    def _eligible(channels: Collection[str], now: datetime) -> tuple:
        return (
            NoticeModel.channel_id.in_(channels),
            NoticeModel.deleted_at.is_(None),
            NoticeModel.deadline_at > now,
            NoticeModel.title.is_not(None),
            NoticeModel.summary.is_not(None),
            NoticeModel.processing_status.in_(("processed", "processing_failed")),
        )

    def finish_delivery(
        self,
        claim: DeliveryClaim,
        content_hash: str,
        channel_id: str,
        message_ts: str,
    ) -> None:
        with self._session_factory() as session, session.begin():
            result = session.execute(
                update(DailyChecklistMessageModel)
                .where(
                    DailyChecklistMessageModel.id == claim.board.id,
                    DailyChecklistMessageModel.lease_token == claim.token,
                )
                .values(
                    status="sent",
                    dm_channel_id=channel_id,
                    message_ts=message_ts,
                    content_hash=content_hash,
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=None,
                    last_error=None,
                )
            )
        if claim.message_ts is None or result.rowcount != 1:
            log_event(
                logger,
                logging.INFO if result.rowcount == 1 else logging.WARNING,
                "checklist_delivery_saved"
                if result.rowcount == 1
                else "checklist_delivery_save_conflict",
                trace_id=claim.token.hex,
                entry_point="checklist_sync",
                daily_id=str(claim.board.id),
                user_ref=identifier_ref(claim.user_id),
                channel_ref=identifier_ref(channel_id),
                message_ts=timestamp_for_log(message_ts),
                rows_updated=result.rowcount,
            )

    def fail_delivery(
        self, claim: DeliveryClaim, error: ChecklistDeliveryError, now: datetime
    ) -> None:
        uncertain = error.uncertain and claim.message_ts is None
        with self._session_factory() as session, session.begin():
            session.execute(
                update(DailyChecklistMessageModel)
                .where(
                    DailyChecklistMessageModel.id == claim.board.id,
                    DailyChecklistMessageModel.lease_token == claim.token,
                )
                .values(
                    status="uncertain" if uncertain else "retry",
                    last_error=error.code[:255],
                    lease_token=None,
                    lease_until=None,
                    next_attempt_at=None
                    if uncertain
                    else now + timedelta(seconds=error.retry_after),
                )
            )

    def apply_action(
        self,
        recipient: ChecklistRecipient,
        channels: Collection[str],
        daily_id: UUID,
        channel_id: str,
        message_ts: str,
        operation: str,
        item_id: UUID | None,
        now: datetime,
        *,
        trace_id: str | None = None,
    ) -> UUID:
        with self._session_factory() as session, session.begin():
            # 발송 준비와 같은 잠금 순서로 교착을 피한다.
            student = session.execute(
                select(StudentModel)
                .where(
                    StudentModel.id == recipient.id,
                    StudentModel.workspace_id == recipient.workspace_id,
                    StudentModel.slack_user_id == recipient.user_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if student is None or student.class_number != recipient.class_number:
                log_event(
                    logger,
                    logging.WARNING,
                    "checklist_action_student_changed",
                    trace_id=trace_id,
                    student_found=student is not None,
                    class_matches=student is not None
                    and student.class_number == recipient.class_number,
                )
                raise ChecklistActionError(
                    "가입 정보가 바뀌었습니다. 최신 체크리스트를 확인해 주세요."
                )
            daily = self._message_for_student(session, student.id, daily_id=daily_id)
            payload_daily = daily
            reason = "match"
            if daily is None:
                referenced = session.get(DailyChecklistMessageModel, daily_id)
                reason = "delivery_not_found" if referenced is None else "delivery_owner_mismatch"
            elif daily.dm_channel_id != channel_id:
                reason = "channel_mismatch"
            elif not _same_message_timestamp(daily.message_ts, message_ts):
                reason = "timestamp_mismatch"
            if (
                daily is None
                or daily.dm_channel_id != channel_id
                or not _same_message_timestamp(daily.message_ts, message_ts)
            ):
                daily = self._message_by_address(session, student.id, channel_id, message_ts)
            rejected = (
                daily is None
                or daily.dm_channel_id != channel_id
                or not _same_message_timestamp(daily.message_ts, message_ts)
            )
            candidates = []
            if rejected:
                # 다른 학생의 값은 기록하지 않는다. 누락/재생성 여부를 볼 소수의 기록만 조회한다.
                candidates = session.scalars(
                    select(DailyChecklistMessageModel)
                    .where(DailyChecklistMessageModel.student_id == student.id)
                    .order_by(DailyChecklistMessageModel.message_date.desc())
                    .limit(3)
                ).all()
            log_event(
                logger,
                logging.WARNING if rejected else logging.INFO,
                "checklist_message_validation",
                trace_id=trace_id,
                result="rejected"
                if rejected
                else ("payload_match" if reason == "match" else "address_fallback"),
                reason=reason,
                student_id=str(student.id),
                payload_daily_id=str(daily_id),
                matched_daily_id=str(daily.id) if daily else None,
                payload_record_owned=payload_daily is not None,
                received_channel_ref=identifier_ref(channel_id),
                received_message_ts=timestamp_for_log(message_ts),
                stored_channel_ref=identifier_ref(payload_daily.dm_channel_id)
                if payload_daily
                else None,
                stored_message_ts=timestamp_for_log(payload_daily.message_ts)
                if payload_daily
                else None,
                channel_matches=payload_daily.dm_channel_id == channel_id
                if payload_daily
                else None,
                timestamp_matches=_same_message_timestamp(payload_daily.message_ts, message_ts)
                if payload_daily
                else None,
                record_status=payload_daily.status if payload_daily else None,
                student_records_sample=[
                    {
                        "daily_id": str(row.id),
                        "channel_ref": identifier_ref(row.dm_channel_id),
                        "message_ts": timestamp_for_log(row.message_ts),
                        "status": row.status,
                    }
                    for row in candidates
                ],
            )
            if rejected:
                raise ChecklistActionError(
                    "현재 연결된 본인의 체크리스트에서만 변경할 수 있습니다."
                )
            if operation in {"complete", "undo"}:
                checklist = session.execute(
                    select(ChecklistModel)
                    .join(NoticeModel)
                    .where(
                        ChecklistModel.id == item_id,
                        ChecklistModel.student_id == student.id,
                        ChecklistModel.deleted_at.is_(None),
                        NoticeModel.workspace_id == recipient.workspace_id,
                        *self._eligible(channels, now),
                    )
                    .with_for_update(of=ChecklistModel)
                ).scalar_one_or_none()
                if checklist is None:
                    log_event(
                        logger,
                        logging.WARNING,
                        "checklist_action_item_unavailable",
                        trace_id=trace_id,
                        daily_id=str(daily.id),
                        item_id=str(item_id) if item_id else None,
                    )
                    raise ChecklistActionError(
                        "마감되었거나 삭제된 항목입니다. 목록을 새로고침해 주세요."
                    )
                if operation == "complete" and checklist.completed_at is None:
                    checklist.completed_at = now
                elif operation == "undo":
                    checklist.completed_at = None
            elif operation in {"pending", "completed"}:
                daily.show_completed = operation == "completed"
                daily.page = 0
            elif operation in {"previous", "next"}:
                daily.page = max(0, daily.page + (1 if operation == "next" else -1))
            elif operation != "refresh":
                raise ChecklistActionError("지원하지 않는 동작입니다.")
            return daily.id
