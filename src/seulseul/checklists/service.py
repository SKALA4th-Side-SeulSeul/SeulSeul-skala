"""개인별 최초 체크리스트 전송과 같은 메시지의 갱신·상태 변경 업무 규칙."""

import hashlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from threading import RLock
from typing import Protocol
from uuid import UUID, uuid4

from seulseul.checklists.diagnostics import log_event
from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistRecipient,
    DailyChecklistBoard,
)
from seulseul.checklists.reminder_repository import Reminder
from seulseul.checklists.reminders import ReminderService
from seulseul.checklists.repository import SqlAlchemyChecklistRepository

logger = logging.getLogger(__name__)
# 문구·서식만 바뀌어도 기존 DM에 한 번 반영한다.
BOARD_PRESENTATION_VERSION = 10


class ChecklistMessenger(Protocol):
    def send(self, user_id: str, board: DailyChecklistBoard) -> tuple[str, str]: ...
    def update(self, channel_id: str, message_ts: str, board: DailyChecklistBoard) -> None: ...
    def delete_previous_messages(self, workspace_id: str, user_id: str) -> None: ...
    def send_reminder(self, reminder: Reminder) -> tuple[str, str]: ...
    def update_reminder(self, reminder: Reminder) -> None: ...
    def delete_reminder(self, channel_id: str, message_ts: str) -> None: ...


def board_hash(board: DailyChecklistBoard) -> str:
    content = asdict(board)
    content.pop("refreshed_at")
    content["presentation_version"] = BOARD_PRESENTATION_VERSION
    return hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()


class DailyChecklistService:
    def __init__(
        self,
        repository: SqlAlchemyChecklistRepository,
        messenger: ChecklistMessenger,
        workspace_id: str,
        targets: Mapping[str, int | None],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._repository = repository
        self._messenger = messenger
        self._workspace_id = workspace_id
        self._targets = dict(targets)
        self._clock = clock
        self._reminders = ReminderService(repository.reminders, messenger, clock)
        # 단일 봇에서 같은 학생의 처리만 직렬화한다. 다른 학생의 버튼은 막지 않는다.
        self._delivery_locks = {}

    def _student_lock(self, workspace_id, user_id):
        return self._delivery_locks.setdefault((workspace_id, user_id), RLock())

    @contextmanager
    def profile_update(self, workspace_id: str, user_id: str) -> Iterator[None]:
        """반 변경과 기존 DM 편집을 직렬화한다. 메시지·발송 기록을 삭제하지 않는다."""
        if workspace_id != self._workspace_id:
            raise ChecklistDeliveryError("invalid_workspace")
        with self._student_lock(workspace_id, user_id):
            yield

    @contextmanager
    def reset_messages(self, workspace_id: str, user_id: str) -> Iterator[None]:
        if workspace_id != self._workspace_id:
            raise ChecklistDeliveryError("invalid_workspace")
        with self._student_lock(workspace_id, user_id):
            self._repository.reset_messages(workspace_id, user_id, finished=False)
            self._messenger.delete_previous_messages(workspace_id, user_id)
            yield
            self._repository.reset_messages(workspace_id, user_id, finished=True)

    def _channels(self, recipient: ChecklistRecipient) -> tuple[str, ...]:
        return tuple(
            channel
            for channel, target in self._targets.items()
            if target is None or target == recipient.class_number
        )

    def run_due(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        for recipient in self._repository.recipients(self._workspace_id):
            if should_stop():
                break
            try:
                self._synchronize(recipient)
            except Exception as error:
                # 사용자 본문/토큰을 포함할 수 있는 예외 문자열은 로그에 남기지 않는다.
                logger.error("개인 체크리스트 동기화 오류: type=%s", type(error).__name__)

    def _synchronize(self, recipient: ChecklistRecipient, daily_id: UUID | None = None) -> bool:
        with self._student_lock(recipient.workspace_id, recipient.user_id):
            try:
                return self._synchronize_locked(recipient, daily_id=daily_id)
            finally:
                if daily_id is None:  # 버튼은 전체 알림 동기화를 하지 않는다.
                    self._sync_reminders(recipient)

    def _sync_reminders(self, recipient, item_id=None):
        try:
            self._reminders.synchronize(recipient, self._channels(recipient), item_id)
        except Exception as error:
            logger.error("마감 알림 동기화 오류: type=%s", type(error).__name__)

    def close_reminder(self, workspace_id, user_id, channel_id, message_ts, value):
        if workspace_id != self._workspace_id:
            raise ChecklistActionError("이 워크스페이스의 알림이 아닙니다.")
        try:
            item_id = UUID(value)
        except (ValueError, TypeError, AttributeError) as error:
            raise ChecklistActionError("알림 정보를 확인할 수 없습니다.") from error
        with self._student_lock(workspace_id, user_id):
            recipient = self._repository.recipient(workspace_id, user_id)
            if recipient is None:
                raise ChecklistActionError("현재 가입되어 있지 않습니다.")
            self._repository.reminders.close(recipient, item_id, channel_id, message_ts)
            self._sync_reminders(recipient, item_id)

    def _synchronize_locked(
        self, recipient: ChecklistRecipient, *, daily_id: UUID | None = None
    ) -> bool:
        now = self._clock().astimezone(timezone.utc)
        claim = self._repository.prepare_delivery(
            recipient,
            self._channels(recipient),
            now,
            class_channels=tuple(
                channel
                for channel, target in self._targets.items()
                if target == recipient.class_number
            ),
            daily_id=daily_id,
        )
        if claim is None:
            return False
        fingerprint = board_hash(claim.board)
        try:
            if claim.channel_id and claim.message_ts:
                if fingerprint != claim.previous_hash:
                    self._messenger.update(claim.channel_id, claim.message_ts, claim.board)
                channel, ts = claim.channel_id, claim.message_ts
            else:
                channel, ts = self._messenger.send(recipient.user_id, claim.board)
        except ChecklistDeliveryError as error:
            self._repository.fail_delivery(claim, error, self._clock())
            logger.warning("개인 DM 갱신 실패: delivery=%s code=%s", claim.board.id, error.code)
            return False
        self._repository.finish_delivery(claim, fingerprint, channel, ts)
        return True

    def handle_action(
        self,
        workspace_id: str,
        user_id: str,
        channel_id: str,
        message_ts: str,
        operation: str,
        value: str,
        *,
        trace_id: str | None = None,
    ) -> bool:
        with self._student_lock(workspace_id, user_id):
            return self._handle_action_locked(
                workspace_id,
                user_id,
                channel_id,
                message_ts,
                operation,
                value,
                trace_id=trace_id or uuid4().hex,
            )

    def _handle_action_locked(
        self,
        workspace_id: str,
        user_id: str,
        channel_id: str,
        message_ts: str,
        operation: str,
        value: str,
        *,
        trace_id: str,
    ) -> bool:
        if workspace_id != self._workspace_id:
            log_event(
                logger, logging.WARNING, "checklist_action_invalid_workspace", trace_id=trace_id
            )
            raise ChecklistActionError("이 워크스페이스의 체크리스트가 아닙니다.")
        now = self._clock().astimezone(timezone.utc)
        try:
            payload = json.loads(value)
            daily_id = UUID(payload["daily"])
            item_id = UUID(payload["item"]) if operation in {"complete", "undo"} else None
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            log_event(logger, logging.WARNING, "checklist_action_invalid_value", trace_id=trace_id)
            raise ChecklistActionError("버튼 정보를 확인할 수 없습니다.") from error
        log_event(
            logger,
            logging.INFO,
            "checklist_action_parsed",
            trace_id=trace_id,
            daily_id=str(daily_id),
            item_id=str(item_id) if item_id else None,
        )
        recipient = self._repository.recipient(workspace_id, user_id)
        if recipient is None:
            log_event(
                logger, logging.WARNING, "checklist_action_student_not_found", trace_id=trace_id
            )
            raise ChecklistActionError(
                "현재 가입되어 있지 않습니다. /seulseul 시작으로 가입해 주세요."
            )
        effective_daily_id = self._repository.apply_action(
            recipient,
            self._channels(recipient),
            daily_id,
            channel_id,
            message_ts,
            operation,
            item_id,
            now,
            trace_id=trace_id,
        )
        if operation == "complete":
            self._sync_reminders(recipient, item_id)
        updated = self._synchronize(recipient, daily_id=effective_daily_id)
        log_event(
            logger,
            logging.INFO,
            "checklist_action_result",
            trace_id=trace_id,
            daily_id=str(effective_daily_id),
            saved=True,
            dm_synchronized=updated,
        )
        return updated
