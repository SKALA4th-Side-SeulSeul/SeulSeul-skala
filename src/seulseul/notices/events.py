"""Slack 원본 메시지 식별자와 이벤트 버전을 검증한다."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

TIMESTAMP = re.compile(r"[0-9]{1,12}\.[0-9]{1,6}\Z")


@dataclass(frozen=True)
class NoticeMessageEvent:
    channel_id: str
    message_ts: str
    revision: Decimal
    kind: Literal["created", "changed", "deleted"]
    text: str


def parse_notice_event(
    event: Mapping[str, Any],
    allowed_channels: Collection[str],
    bot_user_id: str | None,
    bot_id: str | None = None,
) -> NoticeMessageEvent | None:
    """수정은 message.ts, 삭제는 deleted_ts를 원본 식별자로 사용한다.

    수정·삭제 payload에는 channel_type/previous_message가 없을 수 있다.
    이 경우에도 설정된 채널로 전달된 message 이벤트만 허용한다(D-019).
    """
    channel = event.get("channel")
    if event.get("type") != "message" or not isinstance(channel, str):
        return None
    if channel not in allowed_channels:
        return None
    subtype = event.get("subtype")
    if subtype == "message_changed":
        kind = "changed"
        message = event.get("message")
    elif subtype == "message_deleted":
        kind = "deleted"
        message = event.get("previous_message", {})
    elif subtype in (None, "bot_message"):
        kind = "created"
        message = event
    else:
        return None
    if not isinstance(message, Mapping):
        return None
    if event.get("channel_type") not in ("channel", "group"):
        if kind == "created" or event.get("channel_type") is not None:
            return None
    message_ts = event.get("deleted_ts") if kind == "deleted" else message.get("ts")
    if not _timestamp(message_ts):
        return None
    if kind == "deleted" and message.get("ts", message_ts) != message_ts:
        return None
    if message.get("thread_ts", message_ts) not in (None, message_ts):
        return None
    if (bot_user_id and message.get("user") == bot_user_id) or (
        bot_id and message.get("bot_id") == bot_id
    ):
        return None
    if kind != "deleted" and message.get("subtype") not in (None, "bot_message"):
        return None
    text = "" if kind == "deleted" else message.get("text")
    if not isinstance(text, str):
        return None
    if kind == "created" and not text.strip():
        return None
    edited = message.get("edited")
    edited_ts = edited.get("ts") if isinstance(edited, Mapping) else None
    if kind == "deleted":
        revision = event.get("event_ts") or event.get("ts")
    elif kind == "changed":
        revision = edited_ts or event.get("event_ts") or event.get("ts")
    else:
        revision = edited_ts or message_ts
    if not _timestamp(revision) or Decimal(revision) < Decimal(message_ts):
        return None
    return NoticeMessageEvent(channel, message_ts, Decimal(revision), kind, text.strip())


def _timestamp(value: Any) -> bool:
    return (
        isinstance(value, str)
        and TIMESTAMP.fullmatch(value) is not None
        and Decimal(value) < Decimal("253402300799")
    )
