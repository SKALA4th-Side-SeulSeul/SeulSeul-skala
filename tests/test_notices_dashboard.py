"""운영 대시보드의 사람용 공지 상태 표시를 확인한다."""

from datetime import datetime
from zoneinfo import ZoneInfo

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.dashboard import render_dashboard
from seulseul.notices.model import Notice

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=SEOUL)


def notice(*, retry_count=0, next_retry_at=None, title="설문 제출") -> Notice:
    return Notice(
        workspace_id="T0000000001",
        channel_id="C0000000001",
        message_ts="1789344000.000100",
        text="공지 https://forms.example.test/task",
        original_url="https://forms.example.test/task",
        canonical_url="https://forms.example.test/task",
        source_permalink="https://workspace.slack.com/archives/C0000000001/p1789344000000100",
        posted_at=NOW,
        processing_status="processing_failed",
        analysis=NoticeAnalysis(
            title=title,
            summary="설문을 제출합니다.",
            deadline_at=datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL),
            deadline_source_text="9월 30일",
        ),
        retry_count=retry_count,
        last_error="AI 요청 시간 초과",
        next_retry_at=next_retry_at,
    )


def test_dashboard_separates_scheduled_and_manual_failures_without_internal_ids():
    output = render_dashboard(
        [
            notice(next_retry_at=datetime(2026, 9, 22, 12, 5, tzinfo=SEOUL)),
            notice(retry_count=3, title="문서 제출"),
        ],
        [("T0000000001", "C0000000001", "1789344000.000100")],
        [notice(retry_count=0)],
        now=NOW,
    )

    assert "자동 재시도 대기: 1건" in output
    assert "수동 조치 필요: 1건" in output
    assert "미적용 원본: 1건" in output
    assert "1. 설문 제출 · 자동 재시도 대기" in output
    assert "2. 문서 제출 · 수동 조치 필요" in output
    assert "T0000000001" not in output
    assert "1789344000.000100" not in output


def test_dashboard_explains_empty_action_state():
    output = render_dashboard([], [], [], now=NOW)

    assert "수동 조치 필요: 0건" in output
    assert "자동 재시도 대기: 0건" in output
    assert "미적용 원본: 0건" in output
    assert "현재 수동 조치가 필요한 공지가 없습니다." in output
