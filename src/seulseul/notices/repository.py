"""공지 저장소 구현.

서비스가 SQLAlchemy 세션을 직접 다루지 않도록 영속화와 모델 변환을 맡는다.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seulseul.ai.model import NoticeAnalysis
from seulseul.models import MAPPED_MODELS
from seulseul.notices.model import Notice, NoticeModel, ProcessingStatus

_MAPPED_MODELS = MAPPED_MODELS


class NoticeRepository(Protocol):
    """공지 서비스가 사용하는 저장소 계약."""

    def contains(self, workspace_id: str, canonical_url: str) -> bool: ...

    def add(self, notice: Notice) -> bool: ...

    def recent(self, limit: int) -> list[Notice]: ...


class InMemoryNoticeRepository:
    """DB 없이 도메인 동작을 검증할 때 사용하는 메모리 저장소."""

    def __init__(self, max_stored_notices: int) -> None:
        self._notices: deque[Notice] = deque(maxlen=max_stored_notices)
        self._canonical_urls: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def contains(self, workspace_id: str, canonical_url: str) -> bool:
        with self._lock:
            return (workspace_id, canonical_url) in self._canonical_urls

    def add(self, notice: Notice) -> bool:
        identity = (notice.workspace_id, notice.canonical_url)
        with self._lock:
            if identity in self._canonical_urls:
                return False
            if len(self._notices) == self._notices.maxlen:
                removed = self._notices[-1]
                self._canonical_urls.discard((removed.workspace_id, removed.canonical_url))
            self._notices.appendleft(notice)
            self._canonical_urls.add(identity)
            return True

    def recent(self, limit: int) -> list[Notice]:
        with self._lock:
            return list(self._notices)[:limit]


class SqlAlchemyNoticeRepository:
    """PostgreSQL에 공지를 저장하고 조회한다."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def contains(self, workspace_id: str, canonical_url: str) -> bool:
        statement = (
            select(NoticeModel.id)
            .where(
                NoticeModel.workspace_id == workspace_id,
                NoticeModel.canonical_url == canonical_url,
            )
            .limit(1)
        )
        with self._session_factory() as session:
            return session.execute(statement).scalar_one_or_none() is not None

    def add(self, notice: Notice) -> bool:
        statement = (
            insert(NoticeModel)
            .values(**_notice_values(notice))
            .on_conflict_do_nothing(constraint="uq_notices_workspace_canonical_url")
            .returning(NoticeModel.id)
        )
        with self._session_factory() as session:
            inserted_id = session.execute(statement).scalar_one_or_none()
            session.commit()
            return inserted_id is not None

    def recent(self, limit: int) -> list[Notice]:
        statement = (
            select(NoticeModel)
            .where(NoticeModel.deleted_at.is_(None))
            .order_by(NoticeModel.posted_at.desc(), NoticeModel.created_at.desc())
            .limit(limit)
        )
        with self._session_factory() as session:
            models = session.execute(statement).scalars().all()
            return [_to_notice(model) for model in models]


def _notice_values(notice: Notice) -> dict[str, object]:
    analysis = notice.analysis
    return {
        "workspace_id": notice.workspace_id,
        "channel_id": notice.channel_id,
        "message_ts": notice.message_ts,
        "source_text": notice.text,
        "original_url": notice.original_url,
        "canonical_url": notice.canonical_url,
        "source_permalink": notice.source_permalink,
        "posted_at": notice.posted_at,
        "title": analysis.title if analysis is not None else None,
        "summary": analysis.summary if analysis is not None else None,
        "deadline_at": analysis.deadline_at if analysis is not None else None,
        "deadline_source_text": analysis.deadline_source_text if analysis is not None else None,
        "processing_status": notice.processing_status,
        "retry_count": notice.retry_count,
        "last_error": notice.last_error,
        "next_retry_at": notice.next_retry_at,
        "deleted_at": notice.deleted_at,
    }


def _to_notice(model: NoticeModel) -> Notice:
    analysis = _to_analysis(model)
    return Notice(
        workspace_id=model.workspace_id,
        channel_id=model.channel_id,
        message_ts=model.message_ts,
        text=model.source_text,
        original_url=model.original_url,
        canonical_url=model.canonical_url,
        source_permalink=model.source_permalink,
        posted_at=model.posted_at,
        processing_status=cast(ProcessingStatus, model.processing_status),
        analysis=analysis,
        retry_count=model.retry_count,
        last_error=model.last_error,
        next_retry_at=model.next_retry_at,
        deleted_at=model.deleted_at,
    )


def _to_analysis(model: NoticeModel) -> NoticeAnalysis | None:
    title = model.title
    summary = model.summary
    deadline_at = model.deadline_at
    deadline_source_text = model.deadline_source_text
    if title is None or summary is None or deadline_at is None or deadline_source_text is None:
        return None
    return NoticeAnalysis(
        title=title,
        summary=summary,
        deadline_at=deadline_at,
        deadline_source_text=deadline_source_text,
    )
