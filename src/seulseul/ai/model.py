"""AI가 공지에서 추출한 구조화된 결과."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class NoticeAnalysis:
    title: str
    summary: str
    deadline_at: datetime
    deadline_source_text: str
