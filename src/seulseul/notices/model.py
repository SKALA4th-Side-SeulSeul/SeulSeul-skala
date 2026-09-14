"""공지 도메인의 메모리 데이터 구조."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from seulseul.ai.model import NoticeAnalysis

ProcessingStatus = Literal["processed", "processing_failed", "ai_disabled"]


@dataclass(frozen=True)
class Notice:
    workspace_id: str
    channel_id: str
    message_ts: str
    text: str
    original_url: str
    canonical_url: str
    source_permalink: str
    posted_at: datetime
    processing_status: ProcessingStatus
    analysis: NoticeAnalysis | None = None
    last_error: str | None = None
