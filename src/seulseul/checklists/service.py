"""개인별 일일 체크리스트와 전송·상태 변경 업무 규칙."""

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from seulseul.checklists.model import (
    ChecklistActionError,
    ChecklistDeliveryError,
    ChecklistRecipient,
    DailyChecklistBoard,
)
from seulseul.checklists.repository import SqlAlchemyChecklistRepository

logger = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")


class ChecklistMessenger(Protocol):
    def send(self, user_id: str, board: DailyChecklistBoard) -> tuple[str, str]: ...
    def update(self, channel_id: str, message_ts: str, board: DailyChecklistBoard) -> None: ...


def board_hash(board: DailyChecklistBoard) -> str:
    content = asdict(board)
    content.pop("refreshed_at")
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
        notify: Callable[[], None] = lambda: None,
    ) -> None:
        self._repository = repository
        self._messenger = messenger
        self._workspace_id = workspace_id
        self._targets = dict(targets)
        self._clock = clock
        self._notify = notify

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
                logger.error("일일 체크리스트 동기화 오류: type=%s", type(error).__name__)

    def _synchronize(self, recipient: ChecklistRecipient) -> bool:
        now = self._clock().astimezone(timezone.utc)
        local = now.astimezone(SEOUL)
        message_date = local.date() if local.hour >= 9 else None
        claim = self._repository.prepare_delivery(
            recipient, self._channels(recipient), message_date, now
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
            logger.warning("일일 DM 갱신 실패: delivery=%s code=%s", claim.board.id, error.code)
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
    ) -> bool:
        if workspace_id != self._workspace_id:
            raise ChecklistActionError("이 워크스페이스의 체크리스트가 아닙니다.")
        now = self._clock().astimezone(timezone.utc)
        local = now.astimezone(SEOUL)
        if local.hour < 9:
            raise ChecklistActionError("오늘 체크리스트는 오전 9시에 도착합니다.")
        try:
            payload = json.loads(value)
            daily_id = UUID(payload["daily"])
            item_id = UUID(payload["item"]) if operation in {"complete", "undo"} else None
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise ChecklistActionError("버튼 정보를 확인할 수 없습니다.") from error
        recipient = self._repository.recipient(workspace_id, user_id)
        if recipient is None:
            raise ChecklistActionError(
                "현재 가입되어 있지 않습니다. /seulseul 시작으로 가입해 주세요."
            )
        self._repository.apply_action(
            recipient,
            self._channels(recipient),
            daily_id,
            channel_id,
            message_ts,
            operation,
            item_id,
            local.date(),
            now,
        )
        self._notify()
        return self._synchronize(recipient)
