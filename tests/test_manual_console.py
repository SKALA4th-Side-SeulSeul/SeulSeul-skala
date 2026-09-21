"""통합 운영자 공지 콘솔의 등록·삭제 흐름을 확인한다."""

from datetime import datetime
from zoneinfo import ZoneInfo

from seulseul.notices.manual import build_manual_event
from seulseul.notices.repository import InMemoryNoticeRepository
from seulseul.notices.service import NoticeService

SEOUL = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://workspace.slack.com/archives/C123ABC4567/p1789559318987269"


def test_console_manually_registers_notice_without_ai():
    from seulseul.notices.manual_console import run_interactive

    service = NoticeService({"C123ABC4567"}, repository=InMemoryNoticeRepository(20))
    answers = iter(
        [
            "1",
            "T123ABC4567",
            SOURCE_URL,
            "공지 내용 https://forms.gle/example",
            ".done",
            "n",
            "설문 제출",
            "설문에 참여해 주세요.",
            "2026-09-30 23:59",
            "9월 30일 23:59까지",
            "y",
        ]
    )

    assert (
        run_interactive(service, allowed_channels={"C123ABC4567"}, read=lambda _: next(answers))
        == 0
    )
    notice = service.recent_notices(1)[0]
    assert notice.processing_status == "processed"
    assert notice.analysis.title == "설문 제출"
    assert notice.analysis.deadline_at == datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL)


def test_console_deletes_all_links_from_selected_source():
    from seulseul.notices.manual_console import run_interactive

    repository = InMemoryNoticeRepository(20)
    service = NoticeService({"C123ABC4567"}, repository=repository)
    service.record_channel_message(
        build_manual_event(
            "C123ABC4567",
            "1789559318.987269",
            "공지 https://forms.gle/one https://docs.google.com/document/d/two",
            kind="created",
        ),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        collection_error="수동 삭제 테스트",
    )
    answers = iter(["3", "1", "y"])

    assert (
        run_interactive(service, allowed_channels={"C123ABC4567"}, read=lambda _: next(answers))
        == 0
    )
    assert all(
        notice.deleted_at is not None
        for notice in repository.source_notices("T123ABC4567", "C123ABC4567", "1789559318.987269")
    )


def test_console_manually_registers_each_link_from_one_source_separately():
    from seulseul.notices.manual_console import run_interactive

    service = NoticeService({"C123ABC4567"}, repository=InMemoryNoticeRepository(20))
    answers = iter(
        [
            "1",
            "T123ABC4567",
            SOURCE_URL,
            "공지 https://forms.gle/one https://docs.google.com/document/d/two",
            ".done",
            "n",
            "폼 제목",
            "폼 요약",
            "2026-09-30 23:59",
            "9월 30일",
            "문서 제목",
            "문서 요약",
            "2026-10-01 23:59",
            "10월 1일",
            "y",
        ]
    )

    assert (
        run_interactive(service, allowed_channels={"C123ABC4567"}, read=lambda _: next(answers))
        == 0
    )
    notices = service.recent_notices(10)
    assert {notice.analysis.title for notice in notices} == {"폼 제목", "문서 제목"}
