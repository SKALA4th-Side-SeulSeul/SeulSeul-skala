"""체크리스트 배정·개인 상태와 최초 DM의 전송·갱신 기록을 저장한다."""

import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, select, update
from sqlalchemy.orm import Session

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
                    select(NoticeModel.id)
                    .where(
                        NoticeModel.workspace_id == recipient.workspace_id,
                        NoticeModel.processing_status == "processed",
                        *eligible,
                    )
                    .with_for_update(read=True)
                )
                .scalars()
                .all()
            )
            assigned = set(
                session.execute(
                    select(ChecklistModel.notice_id).where(
                        ChecklistModel.student_id == recipient.id,
                    )
                ).scalars()
            )
            session.add_all(
                [
                    ChecklistModel(student_id=recipient.id, notice_id=notice_id)
                    for notice_id in notices
                    if notice_id not in assigned
                ]
            )
            session.flush()

            daily = self._message_for_student(session, recipient.id)
            if daily is None:
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
        session: Session, student_id: UUID
    ) -> DailyChecklistMessageModel | None:
        """학생 잠금 아래 최초 발송 메시지를 선택한다. 과거 일일 기록은 삭제하지 않는다."""
        model = DailyChecklistMessageModel
        return session.execute(
            select(model)
            .where(model.student_id == student_id)
            .order_by(
                case(
                    ((model.dm_channel_id.is_not(None)) & (model.message_ts.is_not(None)), 0),
                    (model.status.in_(("sending", "uncertain")), 1),
                    else_=2,
                ),
                model.message_date,
                model.id,
            )
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()

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
            session.execute(
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
    ) -> None:
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
                raise ChecklistActionError(
                    "가입 정보가 바뀌었습니다. 최신 체크리스트를 확인해 주세요."
                )
            daily = self._message_for_student(session, student.id)
            if (
                daily is None
                or daily.id != daily_id
                or daily.dm_channel_id != channel_id
                or daily.message_ts != message_ts
            ):
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
