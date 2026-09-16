"""공지 저장소 구현.

서비스가 SQLAlchemy 세션을 직접 다루지 않도록 영속화와 모델 변환을 맡는다.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from typing import Protocol, cast

from sqlalchemy import select, update
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

    def recent(self, limit: int, workspace_id: str | None = None) -> list[Notice]: ...

    def failed(
        self, limit: int, channel_ids: Collection[str], workspace_id: str | None = None
    ) -> list[Notice]: ...

    def get(self, workspace_id: str, canonical_url: str) -> Notice | None: ...

    def replace_failed(self, original: Notice, result: Notice) -> bool: ...


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

    def recent(self, limit: int, workspace_id: str | None = None) -> list[Notice]:
        with self._lock:
            return [
                notice
                for notice in self._notices
                if notice.deleted_at is None
                and (workspace_id is None or notice.workspace_id == workspace_id)
            ][:limit]

    def failed(
        self, limit: int, channel_ids: Collection[str], workspace_id: str | None = None
    ) -> list[Notice]:
        with self._lock:
            return [
                notice
                for notice in self._notices
                if notice.processing_status == "processing_failed"
                and notice.deleted_at is None
                and notice.channel_id in channel_ids
                and (workspace_id is None or notice.workspace_id == workspace_id)
            ][:limit]

    def get(self, workspace_id: str, canonical_url: str) -> Notice | None:
        with self._lock:
            return next(
                (
                    notice
                    for notice in self._notices
                    if notice.workspace_id == workspace_id and notice.canonical_url == canonical_url
                ),
                None,
            )

    def replace_failed(self, original: Notice, result: Notice) -> bool:
        with self._lock:
            for index, notice in enumerate(self._notices):
                if notice == original and notice.processing_status == "processing_failed":
                    if notice.deleted_at is not None:
                        return False
                    self._notices[index] = result
                    return True
            return False


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

    def recent(self, limit: int, workspace_id: str | None = None) -> list[Notice]:
        statement = select(NoticeModel).where(NoticeModel.deleted_at.is_(None))
        if workspace_id is not None:
            statement = statement.where(NoticeModel.workspace_id == workspace_id)
        statement = statement.order_by(
            NoticeModel.posted_at.desc(), NoticeModel.created_at.desc()
        ).limit(limit)
        with self._session_factory() as session:
            models = session.execute(statement).scalars().all()
            return [_to_notice(model) for model in models]

    def failed(
        self, limit: int, channel_ids: Collection[str], workspace_id: str | None = None
    ) -> list[Notice]:
        statement = select(NoticeModel).where(
            NoticeModel.processing_status == "processing_failed",
            NoticeModel.deleted_at.is_(None),
            NoticeModel.channel_id.in_(channel_ids),
        )
        if workspace_id is not None:
            statement = statement.where(NoticeModel.workspace_id == workspace_id)
        statement = statement.order_by(
            NoticeModel.posted_at.desc(), NoticeModel.created_at.desc()
        ).limit(limit)
        with self._session_factory() as session:
            return [_to_notice(model) for model in session.execute(statement).scalars().all()]

    def get(self, workspace_id: str, canonical_url: str) -> Notice | None:
        statement = select(NoticeModel).where(
            NoticeModel.workspace_id == workspace_id,
            NoticeModel.canonical_url == canonical_url,
        )
        with self._session_factory() as session:
            model = session.execute(statement).scalar_one_or_none()
            return _to_notice(model) if model is not None else None

    def replace_failed(self, original: Notice, result: Notice) -> bool:
        # 분석 중 다른 요청이 성공했거나 원문·삭제 상태가 바뀌면 덮어쓰지 않는다.
        values = _notice_values(result)
        analysis_fields = (
            "title",
            "summary",
            "deadline_at",
            "deadline_source_text",
            "processing_status",
            "retry_count",
            "last_error",
            "next_retry_at",
        )
        statement = (
            update(NoticeModel)
            .where(
                NoticeModel.workspace_id == original.workspace_id,
                NoticeModel.canonical_url == original.canonical_url,
                NoticeModel.channel_id == original.channel_id,
                NoticeModel.message_ts == original.message_ts,
                NoticeModel.source_text == original.text,
                NoticeModel.posted_at == original.posted_at,
                NoticeModel.processing_status == "processing_failed",
                NoticeModel.deleted_at.is_(None),
                NoticeModel.last_error == original.last_error,
                NoticeModel.retry_count == original.retry_count,
            )
            .values(**{field: values[field] for field in analysis_fields})
            .returning(NoticeModel.id)
        )
        with self._session_factory() as session:
            updated_id = session.execute(statement).scalar_one_or_none()
            session.commit()
            return updated_id is not None


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
