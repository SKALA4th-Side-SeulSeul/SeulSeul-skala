"""공지 저장소 구현.

서비스가 SQLAlchemy 세션을 직접 다루지 않도록 영속화와 모델 변환을 맡는다.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, cast

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from seulseul.ai.model import NoticeAnalysis
from seulseul.checklists.repository import set_notice_checklists_deleted
from seulseul.models import MAPPED_MODELS
from seulseul.notices.events import NoticeMessageEvent
from seulseul.notices.model import Notice, NoticeModel, NoticeSourceModel, ProcessingStatus

_MAPPED_MODELS = MAPPED_MODELS


class AmbiguousNoticeError(ValueError):
    """링크만으로 원본을 하나로 특정할 수 없다."""


class NoticeRepository(Protocol):
    """공지 서비스가 사용하는 저장소 계약."""

    def contains(self, workspace_id: str, canonical_url: str) -> bool: ...

    def add(self, notice: Notice) -> bool: ...

    def recent(self, limit: int, workspace_id: str | None = None) -> list[Notice]: ...

    def failed(
        self, limit: int, channel_ids: Collection[str], workspace_id: str | None = None
    ) -> list[Notice]: ...

    def get(
        self,
        workspace_id: str,
        canonical_url: str,
        channel_id: str | None = None,
        message_ts: str | None = None,
    ) -> Notice | None: ...

    def replace_failed(self, original: Notice, result: Notice) -> bool: ...

    def source_notices(
        self, workspace_id: str, channel_id: str, message_ts: str
    ) -> list[Notice]: ...

    def begin_event(self, workspace_id: str, event: NoticeMessageEvent) -> bool: ...

    def apply_event(
        self, workspace_id: str, event: NoticeMessageEvent, notices: list[Notice]
    ) -> list[Notice] | None: ...


class InMemoryNoticeRepository:
    """DB 없이 도메인 동작을 검증할 때 사용하는 메모리 저장소."""

    def __init__(self, max_stored_notices: int) -> None:
        self._notices: deque[Notice] = deque(maxlen=max_stored_notices)
        self._canonical_urls: set[tuple[str, str, str, str]] = set()
        self._lock = threading.Lock()
        self._sources: dict[tuple[str, str, str], tuple[Decimal, bool, bool]] = {}

    def contains(self, workspace_id: str, canonical_url: str) -> bool:
        with self._lock:
            return any(
                key[0] == workspace_id and key[3] == canonical_url for key in self._canonical_urls
            )

    def add(self, notice: Notice) -> bool:
        identity = (*_source_key(notice), notice.canonical_url)
        with self._lock:
            if identity in self._canonical_urls:
                return False
            if len(self._notices) == self._notices.maxlen:
                removed = self._notices[-1]
                self._canonical_urls.discard((*_source_key(removed), removed.canonical_url))
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

    def get(
        self,
        workspace_id: str,
        canonical_url: str,
        channel_id: str | None = None,
        message_ts: str | None = None,
    ) -> Notice | None:
        with self._lock:
            matches = [
                notice
                for notice in self._notices
                if notice.workspace_id == workspace_id
                and notice.canonical_url == canonical_url
                and (channel_id is None or notice.channel_id == channel_id)
                and (message_ts is None or notice.message_ts == message_ts)
            ]
            if len(matches) > 1:
                raise AmbiguousNoticeError(
                    "같은 링크의 원본이 여러 개입니다. --channel-id와 --message-ts를 지정하세요."
                )
            return matches[0] if matches else None

    def replace_failed(self, original: Notice, result: Notice) -> bool:
        with self._lock:
            state = self._sources.get(_source_key(original))
            if state is not None and (state[1] or not state[2]):
                return False
            for index, notice in enumerate(self._notices):
                if notice == original and notice.processing_status == "processing_failed":
                    if notice.deleted_at is not None:
                        return False
                    self._notices[index] = result
                    return True
            return False

    def source_notices(self, workspace_id: str, channel_id: str, message_ts: str) -> list[Notice]:
        with self._lock:
            return [
                n for n in self._notices if _source_key(n) == (workspace_id, channel_id, message_ts)
            ]

    def begin_event(self, workspace_id: str, event: NoticeMessageEvent) -> bool:
        key = (workspace_id, event.channel_id, event.message_ts)
        with self._lock:
            state = self._sources.get(key)
            if state is not None:
                revision, deleted, applied = state
                if revision > event.revision or (
                    revision == event.revision
                    and applied
                    and not (event.kind == "deleted" and not deleted)
                ):
                    return False
                if deleted and not (event.kind == "deleted" and revision == event.revision):
                    return False
            self._sources[key] = (event.revision, event.kind == "deleted", False)
            return True

    def apply_event(
        self, workspace_id: str, event: NoticeMessageEvent, notices: list[Notice]
    ) -> list[Notice] | None:
        key = (workspace_id, event.channel_id, event.message_ts)
        with self._lock:
            if self._sources.get(key) != (event.revision, event.kind == "deleted", False):
                return None
            results = {n.canonical_url: n for n in notices}
            changed = []
            for index, original in enumerate(self._notices):
                if _source_key(original) != key:
                    continue
                result = results.pop(original.canonical_url, None)
                if result is None and original.deleted_at is None:
                    result = replace(original, deleted_at=_event_time(event), next_retry_at=None)
                if result is None:
                    continue
                if result != original:
                    self._notices[index] = result
                    changed.append(result)
            for notice in results.values():
                identity = (*_source_key(notice), notice.canonical_url)
                if identity in self._canonical_urls:
                    continue
                if len(self._notices) == self._notices.maxlen:
                    removed = self._notices[-1]
                    self._canonical_urls.discard((*_source_key(removed), removed.canonical_url))
                self._notices.appendleft(notice)
                self._canonical_urls.add(identity)
                changed.append(notice)
            self._sources[key] = (event.revision, event.kind == "deleted", True)
            return changed


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
            .on_conflict_do_nothing(constraint="uq_notices_source_link")
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

    def get(
        self,
        workspace_id: str,
        canonical_url: str,
        channel_id: str | None = None,
        message_ts: str | None = None,
    ) -> Notice | None:
        statement = select(NoticeModel).where(
            NoticeModel.workspace_id == workspace_id,
            NoticeModel.canonical_url == canonical_url,
        )
        if channel_id is not None:
            statement = statement.where(NoticeModel.channel_id == channel_id)
        if message_ts is not None:
            statement = statement.where(NoticeModel.message_ts == message_ts)
        with self._session_factory() as session:
            models = session.execute(statement.limit(2)).scalars().all()
            if len(models) > 1:
                raise AmbiguousNoticeError(
                    "같은 링크의 원본이 여러 개입니다. --channel-id와 --message-ts를 지정하세요."
                )
            model = models[0] if models else None
            return _to_notice(model) if model is not None else None

    def source_notices(self, workspace_id: str, channel_id: str, message_ts: str) -> list[Notice]:
        with self._session_factory() as session:
            models = (
                session.execute(
                    select(NoticeModel).where(
                        NoticeModel.workspace_id == workspace_id,
                        NoticeModel.channel_id == channel_id,
                        NoticeModel.message_ts == message_ts,
                    )
                )
                .scalars()
                .all()
            )
            return [_to_notice(model) for model in models]

    def begin_event(self, workspace_id: str, event: NoticeMessageEvent) -> bool:
        key = (workspace_id, event.channel_id, event.message_ts)
        with self._session_factory() as session, session.begin():
            session.execute(
                insert(NoticeSourceModel)
                .values(
                    workspace_id=workspace_id,
                    channel_id=event.channel_id,
                    message_ts=event.message_ts,
                    revision=event.revision,
                    deleted=False,
                    applied=False,
                )
                .on_conflict_do_nothing(index_elements=["workspace_id", "channel_id", "message_ts"])
            )
            source = session.get(NoticeSourceModel, key, with_for_update=True)
            if source.revision > event.revision or (
                source.revision == event.revision
                and source.applied
                and not (event.kind == "deleted" and not source.deleted)
            ):
                return False
            if source.deleted and not (
                event.kind == "deleted" and source.revision == event.revision
            ):
                return False
            source.revision = event.revision
            source.deleted = event.kind == "deleted"
            source.applied = False
            return True

    def apply_event(
        self, workspace_id: str, event: NoticeMessageEvent, notices: list[Notice]
    ) -> list[Notice] | None:
        key = (workspace_id, event.channel_id, event.message_ts)
        with self._session_factory() as session, session.begin():
            source = session.get(NoticeSourceModel, key, with_for_update=True)
            if (
                source is None
                or source.revision != event.revision
                or source.applied
                or source.deleted != (event.kind == "deleted")
            ):
                return None
            models = (
                session.execute(
                    select(NoticeModel)
                    .where(
                        NoticeModel.workspace_id == workspace_id,
                        NoticeModel.channel_id == event.channel_id,
                        NoticeModel.message_ts == event.message_ts,
                    )
                    .order_by(NoticeModel.canonical_url)
                    .with_for_update()
                )
                .scalars()
                .all()
            )
            results = {n.canonical_url: n for n in notices}
            changed = []
            for model in models:
                original = _to_notice(model)
                result = results.pop(model.canonical_url, None)
                if result is None:
                    if model.deleted_at is not None:
                        continue
                    result = replace(original, deleted_at=_event_time(event), next_retry_at=None)
                    set_notice_checklists_deleted(session, [model.id], result.deleted_at)
                else:
                    if result.processing_status == "processed" and result.deleted_at is None:
                        set_notice_checklists_deleted(session, [model.id], None)
                if result != original:
                    for name, value in _notice_values(result).items():
                        setattr(model, name, value)
                    changed.append(result)
            # 원본별 링크를 저장한다. 같은 원본 이벤트만 중복을 막는다.
            for notice in sorted(results.values(), key=lambda n: n.canonical_url):
                inserted = session.execute(
                    insert(NoticeModel)
                    .values(**_notice_values(notice))
                    .on_conflict_do_nothing(
                        index_elements=["workspace_id", "channel_id", "message_ts", "canonical_url"]
                    )
                    .returning(NoticeModel.id)
                ).scalar_one_or_none()
                if inserted is not None:
                    changed.append(notice)
            source.applied = True
            return changed

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
            source = session.get(NoticeSourceModel, _source_key(original), with_for_update=True)
            if source is not None and (source.deleted or not source.applied):
                return False
            updated_id = session.execute(statement).scalar_one_or_none()
            if updated_id is not None and result.processing_status == "processed":
                set_notice_checklists_deleted(session, [updated_id], None)
            session.commit()
            return updated_id is not None


def _source_key(notice: Notice) -> tuple[str, str, str]:
    return notice.workspace_id, notice.channel_id, notice.message_ts


def _event_time(event: NoticeMessageEvent) -> datetime:
    return datetime.fromtimestamp(float(event.revision), timezone.utc)


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
