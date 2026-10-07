"""운영 로그 형식 공통 설정.

봇(main)과 운영자 CLI(수동 공지 콘솔 등)가 같은 형식·한국 시간으로 로그를 남겨
`./view.sh logs`에서 함께 읽을 수 있게 한다. Slack·DB 등 무거운 의존성을 가져오지 않는다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

SEOUL_TIMEZONE = ZoneInfo("Asia/Seoul")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class SeoulFormatter(logging.Formatter):
    """서버·컨테이너의 OS 시간대와 관계없이 로그 시각을 한국 시간으로 표시한다."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        timestamp = datetime.fromtimestamp(record.created, SEOUL_TIMEZONE)
        if datefmt:
            return timestamp.strftime(datefmt)
        return f"{timestamp:%Y-%m-%d %H:%M:%S},{int(record.msecs):03d}"


def configure_logging() -> None:
    """애플리케이션 로그의 표시 시간대를 `Asia/Seoul`로 고정한다."""
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    formatter = SeoulFormatter(LOG_FORMAT)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
