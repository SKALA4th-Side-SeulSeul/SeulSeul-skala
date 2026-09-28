"""단일 봇·학생별 잠금 아래 마지막 알림 상태만 보존한다."""

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import and_, or_, select

from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistModel,
    DailyChecklistMessageModel,
)
from seulseul.notices.model import NoticeModel
from seulseul.users.model import StudentModel


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


@dataclass(frozen=True)
class Reminder:
    item_id: UUID
    student_id: UUID
    user_id: str
    operation: str
    channel: str | None
    ts: str | None
    title: str
    url: str
    deadline: datetime
    fingerprint: str


class ReminderRepository:
    def __init__(self, sessions):
        self._sessions = sessions

    def candidates(self, student_id, now):
        with self._sessions() as session:
            return list(
                session.scalars(
                    select(ChecklistModel.id)
                    .join(NoticeModel)
                    .where(
                        ChecklistModel.student_id == student_id,
                        or_(
                            ChecklistModel.reminder_status.in_(["sent", "deleting"]),
                            and_(
                                ChecklistModel.completed_at.is_(None),
                                ChecklistModel.deleted_at.is_(None),
                                NoticeModel.deleted_at.is_(None),
                                NoticeModel.deadline_at > now,
                                NoticeModel.deadline_at <= now + timedelta(hours=24),
                            ),
                        ),
                    )
                    .order_by(ChecklistModel.id)
                )
            )

    def prepare(self, recipient, channels, item_id, now):
        with self._sessions() as session, session.begin():
            student = session.get(StudentModel, recipient.id)
            if student is None or student.class_number != recipient.class_number:
                return None
            if session.scalar(
                select(DailyChecklistMessageModel.id).where(
                    DailyChecklistMessageModel.student_id == recipient.id,
                    DailyChecklistMessageModel.last_error == "dm_cleanup_pending",
                )
            ):
                return None
            item = session.get(ChecklistModel, item_id)
            if item is None or item.student_id != recipient.id:
                return None
            notice = session.get(NoticeModel, item.notice_id)
            deadline = _aware(notice.deadline_at)
            due = (
                notice.workspace_id == recipient.workspace_id
                and notice.channel_id in channels
                and item.completed_at is None
                and item.deleted_at is None
                and notice.deleted_at is None
                and notice.title
                and notice.summary
                and notice.processing_status in {"processed", "processing_failed"}
                and deadline
                and now < deadline <= now + timedelta(hours=24)
            )
            state = item.reminder_status
            if state in {"uncertain", "blocked"}:
                return None
            deleting = state == "deleting" or (state == "sent" and not due)
            if item.reminder_retry_at and _aware(item.reminder_retry_at) > now:
                if not deleting or state == "deleting":
                    return None
            if not deleting and (
                not due or (state == "closed" and _aware(item.reminder_deadline_at) == deadline)
            ):
                return None
            fingerprint = hashlib.sha256(
                repr((notice.title, notice.original_url, deadline)).encode()
            ).hexdigest()
            if deleting:
                operation = "delete"
                item.reminder_status = "deleting"
            elif state == "sent":
                if item.reminder_hash == fingerprint:
                    return None
                operation = "update"
                # Slack 편집 후 DB 저장 전에 중단돼도 현재 내용으로 다시 편집한다.
                item.reminder_hash = None
            else:
                operation = "send"
                item.reminder_status = "uncertain"
                item.reminder_deadline_at = deadline
                item.reminder_channel_id = item.reminder_message_ts = None
            item.reminder_retry_at = None
            return Reminder(
                item.id,
                recipient.id,
                recipient.user_id,
                operation,
                item.reminder_channel_id,
                item.reminder_message_ts,
                notice.title or "할 일",
                notice.original_url,
                deadline or now,
                fingerprint,
            )

    def finish(self, reminder, *, channel=None, ts=None, missing=False):
        with self._sessions() as session, session.begin():
            item = session.get(ChecklistModel, reminder.item_id)
            if item is None or item.student_id != reminder.student_id:
                return
            item.reminder_status = "closed" if missing or reminder.operation == "delete" else "sent"
            if reminder.operation != "delete":
                item.reminder_deadline_at = reminder.deadline
                item.reminder_channel_id = channel or reminder.channel
                item.reminder_message_ts = ts or reminder.ts
                item.reminder_hash = reminder.fingerprint
            item.reminder_retry_at = None

    def fail(self, reminder, error, now):
        with self._sessions() as session, session.begin():
            item = session.get(ChecklistModel, reminder.item_id)
            if item is None or item.student_id != reminder.student_id:
                return
            if reminder.operation == "send" and not error.uncertain:
                item.reminder_status = "pending"
            if error.code in {
                "invalid_auth",
                "token_revoked",
                "missing_scope",
                "account_inactive",
                "channel_not_found",
                "user_not_found",
                "invalid_blocks",
                "invalid_arguments",
                "invalid_dm_response",
                "restricted_action",
                "cant_delete_message",
                "cant_update_message",
                "not_authed",
            }:
                item.reminder_status = "blocked"
            item.reminder_retry_at = now + timedelta(seconds=max(1, error.retry_after))

    def close(self, recipient, item_id, channel, ts):
        with self._sessions() as session, session.begin():
            item = session.get(ChecklistModel, item_id)
            if (
                not channel
                or not ts
                or item is None
                or item.student_id != recipient.id
                or item.reminder_channel_id != channel
                or item.reminder_message_ts != ts
            ):
                raise ChecklistActionError("본인의 현재 마감 알림에서만 닫을 수 있습니다.")
            if item.reminder_status != "closed":
                item.reminder_status = "deleting"
                item.reminder_deadline_at = session.get(NoticeModel, item.notice_id).deadline_at
                item.reminder_retry_at = None
