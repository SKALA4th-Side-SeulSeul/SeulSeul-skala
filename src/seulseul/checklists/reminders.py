"""한 학생의 알림만 동기화한다. 버튼에서는 지정 항목만 처리한다."""

import logging

from seulseul.checklists.model import ChecklistDeliveryError

logger = logging.getLogger(__name__)


class ReminderService:
    def __init__(self, repository, messenger, clock):
        self._repository, self._messenger, self._clock = repository, messenger, clock

    def synchronize(self, recipient, channels, item_id=None):
        ids = [item_id] if item_id else self._repository.candidates(recipient.id, self._clock())
        for item in ids:
            # API 호출 도중 마감/원문이 변경되면 한 번 더 반영한다.
            for _ in range(2):
                reminder = self._repository.prepare(recipient, channels, item, self._clock())
                if reminder is None:
                    break
                try:
                    if reminder.operation == "send":
                        channel, ts = self._messenger.send_reminder(reminder)
                        self._repository.finish(reminder, channel=channel, ts=ts)
                    elif reminder.operation == "update":
                        self._messenger.update_reminder(reminder)
                        self._repository.finish(reminder)
                    else:
                        self._messenger.delete_reminder(reminder.channel, reminder.ts)
                        self._repository.finish(reminder)
                    logger.info("마감 알림 처리: item=%s operation=%s", item, reminder.operation)
                except ChecklistDeliveryError as error:
                    if error.code == "message_not_found" and reminder.operation != "send":
                        self._repository.finish(reminder, missing=True)
                    else:
                        self._repository.fail(reminder, error, self._clock())
                        logger.warning(
                            "마감 알림 실패: item=%s operation=%s code=%s uncertain=%s",
                            item,
                            reminder.operation,
                            error.code,
                            error.uncertain,
                        )
                    if error.code == "ratelimited":
                        return
                    break
