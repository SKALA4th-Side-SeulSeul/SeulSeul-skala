"""운영 대시보드의 사람용 공지 상태 표시를 확인한다."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices import dashboard
from seulseul.notices.dashboard import render_dashboard
from seulseul.notices.model import Notice
from seulseul.notices.screen import display_width

SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=SEOUL)


def notice(
    *,
    retry_count=0,
    next_retry_at=None,
    title="설문 제출",
    processing_status="processing_failed",
    last_error="AI 요청 시간 초과",
    deadline_at=datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL),
) -> Notice:
    return Notice(
        workspace_id="T0000000001",
        channel_id="C0000000001",
        message_ts="1789344000.000100",
        text="공지 https://forms.example.test/task",
        original_url="https://forms.example.test/task",
        canonical_url="https://forms.example.test/task",
        source_permalink="https://workspace.slack.com/archives/C0000000001/p1789344000000100",
        posted_at=NOW,
        processing_status=processing_status,
        analysis=NoticeAnalysis(
            title=title,
            summary="설문을 제출합니다.",
            deadline_at=deadline_at,
            deadline_source_text="9월 30일",
        ),
        retry_count=retry_count,
        last_error=last_error,
        next_retry_at=next_retry_at,
    )


def test_dashboard_shows_counts_ordered_actions_recent_states_and_hides_ids():
    long_title = "매우 긴 한국어 공지 제목으로 최근 공지 표의 표시 폭을 확인합니다"
    output = render_dashboard(
        [
            notice(
                retry_count=3,
                title="문서 제출",
                last_error="AI 요청 실패, 응답: secret nvapi-secret",
            ),
            notice(next_retry_at=datetime(2026, 9, 22, 12, 5, tzinfo=SEOUL)),
        ],
        [("T0000000001", "C0000000001", "1789344000.000100")],
        [
            notice(title="완료한 설문", processing_status="processed"),
            notice(title=long_title, processing_status="ai_disabled"),
        ],
        now=NOW,
    )

    assert "■ 한눈에 보기" in output
    assert "자동 재시도 대기  1건" in output
    assert "수동 조치 필요    1건" in output
    assert "미적용 원본       1건" in output
    assert "■ 처리가 필요한 공지 · 2건" in output
    assert output.index("  1. 문서 제출") < output.index("  2. 설문 제출")
    assert "✖ 수동 조치 필요" in output
    assert "↻ 자동 재시도 대기" in output
    assert "다음 재시도 09/22(화) 12:05 · 0/3회" in output
    assert "› 다시 분석: ./admin.sh retry 1" in output
    assert "✔ 정상" in output
    assert "⚠ 수동 입력 필요" in output
    assert "…" in output
    assert "09/30(수) 23:59" in output
    assert all(
        display_width(line) <= 80
        for line in output.splitlines()
        if "완료한 설문" in line or "매우 긴" in line
    )

    for internal_value in (
        "T0000000001",
        "C0000000001",
        "1789344000.000100",
        "workspace.slack.com",
        "nvapi-secret",
        "secret",
    ):
        assert internal_value not in output


def test_dashboard_explains_empty_action_and_recent_states():
    output = render_dashboard([], [], [], now=NOW)

    assert "자동 재시도 대기  0건" in output
    assert "수동 조치 필요    0건" in output
    assert "미적용 원본       0건" in output
    assert "░" in output
    assert "✔ 7일 안에 마감되는 공지가 없습니다." in output
    assert "  ✔ 지금 처리할 공지가 없습니다." in output
    assert "  • 저장된 공지가 없습니다." in output


def test_dashboard_table_columns_align_by_display_width():
    long_title = "매우 긴 한국어 공지 제목으로 열 위치가 모든 행에서 같은지 확인합니다"
    output = render_dashboard(
        [],
        [],
        [
            notice(title="정상 공지", processing_status="processed"),
            notice(title="AI 재시도", next_retry_at=NOW),
            notice(title=long_title, processing_status="ai_disabled"),
        ],
        now=NOW,
    )
    lines = output.splitlines()
    header = next(line for line in lines if "공지 제목" in line)
    header_title_column = display_width(header[: header.index("공지 제목")])
    header_deadline_column = display_width(header[: header.index("마감")])

    rendered_rows = (
        ("✔ 정상", "정상 공지"),
        ("✖ AI 확인 필요", "AI 재시도"),
        ("⚠ 수동 입력 필요", "매우 긴"),
    )
    for status, title_start in rendered_rows:
        row = next(line for line in lines if title_start in line)
        assert status in row
        assert display_width(row[: row.index(title_start)]) == header_title_column
        deadline_column = display_width(row[: row.index("09/30(수) 23:59")])
        assert deadline_column == header_deadline_column


def test_dashboard_overview_counts_start_in_the_same_display_column():
    output = render_dashboard(
        [notice(next_retry_at=NOW), notice(retry_count=3)],
        [("workspace", "channel", "1789344000.000100")],
        [],
        now=NOW,
    )
    overview = output.split("■ 한눈에 보기", 1)[1].split("\n■", 1)[0]
    rows = (
        next(line for line in overview.splitlines() if "↻ 자동 재시도 대기" in line),
        next(line for line in overview.splitlines() if "✖ 수동 조치 필요" in line),
        next(line for line in overview.splitlines() if "⚠ 미적용 원본" in line),
    )
    count_columns = []
    for line in rows:
        count_match = re.search(r"\d+건", line)
        assert count_match is not None
        count_columns.append(display_width(line[: count_match.start()]))

    assert len(set(count_columns)) == 1


def test_manual_action_hint_uses_the_same_number_after_a_retry_waiting_notice():
    output = render_dashboard(
        [notice(next_retry_at=NOW), notice(retry_count=3)],
        [],
        [],
        now=NOW,
    )

    assert "    › 다시 분석: ./admin.sh retry 2" in output
    assert "    › 다시 분석: ./admin.sh retry 1" not in output


def test_dashboard_adapts_all_lines_to_width_and_renders_both_charts():
    long_title = "한국어로 아주 길게 작성된 공지 제목을 화면 폭에 맞춰 줄이는 검증용 문자열입니다"
    recent = [
        notice(
            title=long_title,
            processing_status="processed",
            deadline_at=datetime(2026, 9, 23, 12, 0, tzinfo=SEOUL),
        ),
        notice(
            title="재시도 공지",
            next_retry_at=datetime(2026, 9, 22, 12, 5, tzinfo=SEOUL),
        ),
        notice(title="수동 조치 공지", retry_count=3),
        notice(title="기타 공지", processing_status="ai_disabled"),
        notice(
            title="다음 주 제출",
            processing_status="processed",
            deadline_at=datetime(2026, 9, 25, 12, 0, tzinfo=SEOUL),
        ),
        notice(
            title="주간 회고",
            processing_status="processed",
            deadline_at=datetime(2026, 9, 28, 12, 0, tzinfo=SEOUL),
        ),
    ]
    for width in (40, 60, 120):
        output = render_dashboard(
            recent[1:3],
            [],
            recent,
            now=NOW,
            width=width,
        )

        assert all(display_width(line) <= width for line in output.splitlines())
        assert "■ 공지 상태 분포 · 최근 6건" in output
        assert "■ 다가오는 마감 · 7일" in output
        assert "█" in output and "░" in output
        assert "09/23(수)" in output
        assert "09/25(금)" in output
        assert "09/28(월)" in output
        assert "1건  17%" in output
        assert "오늘" in output
        assert all(
            not line.endswith("…") for line in output.splitlines() if line.startswith("  ┆ ")
        )


def test_dashboard_panel_is_compact_and_reports_truncated_rows():
    failed = [notice(title=f"처리 대상 공지 {index}", retry_count=3) for index in range(1, 5)]
    recent_titles = [f"최근 공지 {index}" for index in range(1, 8)]
    recent = [notice(title=title, processing_status="processed") for title in recent_titles]

    output = render_dashboard(failed, [], recent, now=NOW, width=80, panel=True)
    lines = output.splitlines()

    assert not any(line.startswith(("━", "─")) for line in lines)
    assert "  … 외 1건 · 전체 보기: ./view.sh dashboard" in output
    assert "  … 외 2건 · 전체 보기: ./view.sh dashboard" in output
    # 패널은 조치 공지를 한 줄 표로 압축하고 차트보다 먼저 보여 준다(작은 창에서 잘리지 않게).
    action_rows = [line for line in lines if line.startswith(("  1  ", "  2  ", "  3  "))]
    assert len(action_rows) == 3
    assert all("✖ 수동 조치 필요" in row and "09/30(수) 23:59" in row for row in action_rows)
    title_columns = {display_width(row[: row.index("처리 대상 공지")]) for row in action_rows}
    assert len(title_columns) == 1
    assert "  › ✖ 공지 다시 분석: 관리 콘솔 메뉴 1 또는 ./admin.sh retry 번호" in output
    assert output.index("■ 처리가 필요한 공지") < output.index("■ 공지 상태 분포")
    assert sum(any(f"최근 공지 {index}" in line for index in range(1, 6)) for line in lines) == 5
    assert all(display_width(line) <= 80 for line in lines)
    for index, line in enumerate(lines):
        if line.startswith("■ "):
            assert lines[index + 1].startswith("  ┆ ")
    assert all(
        lines[index + 1] != ""
        for index, line in enumerate(lines)
        if line.startswith("■ ") and index + 1 < len(lines)
    )


def test_dashboard_main_passes_width_and_panel_and_queries_100_recent_notices(monkeypatch, capsys):
    class FakeEngine:
        disposed = False

        def dispose(self):
            self.disposed = True

    class FakeSettings:
        notice_channels = ("C_CONFIGURED",)
        manual_notice_channels = ("C_MANUAL",)

    class FakeService:
        def __init__(self, channels, *, repository):
            assert channels == ("C_CONFIGURED", "C_MANUAL")
            assert repository is fake_repository
            self.calls = []

        def failed_notices(self, limit):
            return []

        def pending_sources(self, limit):
            return []

        def recent_notices(self, limit, *, configured_only=False):
            self.calls.append((limit, configured_only))
            return []

    fake_engine = FakeEngine()
    fake_repository = object()
    service_instances = []

    def create_service(channels, *, repository):
        service = FakeService(channels, repository=repository)
        service_instances.append(service)
        return service

    original_render = dashboard.render_dashboard
    render_arguments = {}

    def capture_render(*args, **kwargs):
        render_arguments.update(kwargs)
        return original_render(*args, **kwargs)

    monkeypatch.setattr(dashboard, "load_slack_settings", lambda: FakeSettings())
    monkeypatch.setattr(dashboard, "load_database_settings", lambda: object())
    monkeypatch.setattr(dashboard, "create_database_engine", lambda settings: fake_engine)
    monkeypatch.setattr(dashboard, "create_session_factory", lambda engine: object())
    monkeypatch.setattr(dashboard, "SqlAlchemyNoticeRepository", lambda factory: fake_repository)
    monkeypatch.setattr(dashboard, "NoticeService", create_service)
    monkeypatch.setattr(dashboard, "render_dashboard", capture_render)

    assert dashboard.main(["--width", "120", "--panel"]) == 0

    assert "■ 한눈에 보기" in capsys.readouterr().out
    assert service_instances[0].calls == [(100, True)]
    assert render_arguments["width"] == 120
    assert render_arguments["panel"] is True
    assert fake_engine.disposed is True


def test_dashboard_main_rejects_width_outside_supported_range():
    import pytest

    with pytest.raises(SystemExit) as error:
        dashboard.main(["--width", "39"])

    assert error.value.code == 2
