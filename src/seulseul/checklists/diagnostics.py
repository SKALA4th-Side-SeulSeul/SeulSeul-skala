"""체크리스트 버튼 진단 로그. 요청 본문 대신 허용한 식별·비교 정보만 기록한다."""

import hashlib
import json
import logging
import os
import re
from uuid import uuid4

INSTANCE_ID = uuid4().hex[:12]
OPERATIONS = frozenset({"complete", "undo", "pending", "completed", "previous", "next", "refresh"})


def identifier_ref(value: str | None) -> str | None:
    """Slack 식별자 원문을 남기지 않고 프로세스 간 대조 가능한 참조를 만든다."""
    return hashlib.sha256(value.encode()).hexdigest()[:12] if value else None


def timestamp_for_log(value: str | None) -> str | None:
    if value is None:
        return None
    return value if re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,9}", value) else "invalid"


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    trace_id: str | None = None,
    entry_point: str = "slack_button",
    **fields: object,
) -> None:
    """호출부에서 선별한 필드만 JSON으로 직렬화한다. 예외·payload 전체는 금지한다."""
    logger.log(
        level,
        json.dumps(
            {
                "diagnostic_version": 1,
                "event": event,
                "trace_id": trace_id,
                "entry_point": entry_point,
                "instance_id": INSTANCE_ID,
                "pid": os.getpid(),
                **fields,
            },
            ensure_ascii=False,
        ),
    )
