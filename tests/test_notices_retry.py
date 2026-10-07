"""재시도 운영 화면이 내부 식별자 없이 필요한 조치를 안내하는지 확인한다."""

from argparse import Namespace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.model import Notice
from seulseul.notices.retry import run_command

SEOUL = ZoneInfo("Asia/Seoul")


def notice(
    *,
    title="설문 제출",
    retry_count=0,
    next_retry_at=None,
    processing_status="processing_failed",
    last_error="AI 요청 시간 초과",
) -> Notice:
    return Notice(
        workspace_id="T_PRIVATE_WORKSPACE",
        channel_id="C_PRIVATE_CHANNEL",
        message_ts="1789344000.000100",
        text="원문 https://forms.example.test/private-task",
        original_url="https://forms.example.test/private-task",
        canonical_url="https://forms.example.test/private-task",
        source_permalink="https://workspace.slack.com/private-source",
        posted_at=datetime(2026, 9, 14, 9, 0, tzinfo=SEOUL),
        processing_status=processing_status,
        analysis=NoticeAnalysis(
            title=title,
            summary="과제를 제출합니다.",
            deadline_at=datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL),
            deadline_source_text="9월 30일",
        ),
        retry_count=retry_count,
        last_error=last_error,
        next_retry_at=next_retry_at,
    )


class FakeNoticeService:
    def __init__(
        self,
        *,
        notices=(),
        sources=(),
        retry_result=None,
    ) -> None:
        self.notices = list(notices)
        self.sources = list(sources)
        self.retry_result = retry_result
        self.failed_calls = []
        self.pending_calls = []
        self.retry_calls = []

    def failed_notices(self, limit, *, workspace_id=None):
        self.failed_calls.append((limit, workspace_id))
        return self.notices[:limit]

    def pending_sources(self, limit, *, workspace_id=None):
        self.pending_calls.append((limit, workspace_id))
        return self.sources[:limit]

    def retry_failed_notice(self, workspace_id, url, channel_id=None, message_ts=None):
        self.retry_calls.append((workspace_id, url, channel_id, message_ts))
        return self.retry_result


def test_retry_list_numbers_in_service_order_and_hides_internal_values(capsys):
    first = notice(
        title="운영자가 확인할 설문",
        retry_count=3,
        last_error="상태 코드: 500, 응답: private nvapi-secret",
    )
    second = notice(
        title="자동 재시도 예정 공지",
        retry_count=1,
        next_retry_at=datetime(2026, 9, 14, 9, 15, tzinfo=SEOUL),
    )
    service = FakeNoticeService(notices=[first, second])

    exit_code = run_command(service, Namespace(action="list", workspace_id=None, limit=100))
    assert exit_code == 0
    output = capsys.readouterr().out

    assert output.index("  1. 운영자가 확인할 설문") < output.index("  2. 자동 재시도 예정 공지")
    assert "✖ 수동 조치 필요 · 자동 재시도 3/3회 완료" in output
    assert "↻ 자동 재시도 대기 · 다음 재시도 09/14(월) 09:15 · 1/3회" in output
    assert "› 다시 분석: ./admin.sh retry 1" in output
    assert "09/30(수) 23:59" in output
    assert "상태 코드: 500" in output
    assert "private" not in output and "nvapi-secret" not in output
    assert ".venv/bin/python" not in output
    assert "seulseul.notices.retry" not in output
    assert "T_PRIVATE_WORKSPACE" not in output
    assert "C_PRIVATE_CHANNEL" not in output
    assert "1789344000.000100" not in output
    assert "private-task" not in output
    assert service.failed_calls == [(100, None)]


def test_retry_list_limit_hint_uses_limit(capsys):
    service = FakeNoticeService(notices=[notice()])

    assert run_command(service, Namespace(action="list", workspace_id=None, limit=1)) == 0
    output = capsys.readouterr().out

    assert "조회 한도 1건에 도달했습니다." in output
    assert "--limit 값을 높여 다시 조회하세요." in output


def test_pending_shows_seoul_time_channel_location_and_short_guidance(capsys):
    service = FakeNoticeService(
        sources=[("T_PRIVATE_WORKSPACE", "C_LOCATE_SOURCE", "1789344000.000100")]
    )

    assert run_command(service, Namespace(action="pending", workspace_id=None, limit=20)) == 0
    output = capsys.readouterr().out

    assert "09/14(월) 09:00" in output
    assert "Slack 채널 ID  C_LOCATE_SOURCE" in output
    assert "› 이렇게 하세요" in output
    assert "정상 처리 중" in output
    assert "Slack에서 원문을 확인하고 실제로 다시 수정" in output
    assert "삭제된 원본" in output
    assert "DB 초기화" in output
    assert "T_PRIVATE_WORKSPACE" not in output
    assert "1789344000.000100" not in output
    assert "C_PRIVATE_CHANNEL" not in output
    assert service.pending_calls == [(20, None)]


def test_pending_limit_hint_uses_limit(capsys):
    service = FakeNoticeService(sources=[("T_PRIVATE_WORKSPACE", "C_LOCATE_SOURCE", "1789344000")])

    assert run_command(service, Namespace(action="pending", workspace_id=None, limit=1)) == 0
    output = capsys.readouterr().out

    assert "조회 한도 1건에 도달했습니다." in output
    assert "--limit 값을 높여 다시 조회하세요." in output


@pytest.mark.parametrize(
    ("processing_status", "last_error", "expected_exit", "expected_text"),
    [
        ("processed", None, 0, "✔ 재처리 성공"),
        ("processing_failed", "AI 응답, 응답: private nvapi-secret", 1, "✖ 재처리 실패"),
    ],
)
def test_retry_reports_success_or_sanitized_failure(
    capsys, processing_status, last_error, expected_exit, expected_text
):
    result = notice(
        processing_status=processing_status,
        last_error=last_error,
    )
    service = FakeNoticeService(retry_result=result)

    exit_code = run_command(
        service,
        Namespace(
            action="retry",
            index=None,
            workspace_id="T_PRIVATE_WORKSPACE",
            url="https://forms.example.test/private-task",
            channel_id="C_PRIVATE_CHANNEL",
            message_ts="1789344000.000100",
        ),
    )
    output = capsys.readouterr().out

    assert exit_code == expected_exit
    assert expected_text in output
    if expected_exit:
        assert "AI 응답" in output
        assert "private" not in output and "nvapi-secret" not in output
        assert "./admin.sh retry" in output
    else:
        assert "기존 체크리스트 DM에 반영합니다." in output
    assert service.retry_calls == [
        (
            "T_PRIVATE_WORKSPACE",
            "https://forms.example.test/private-task",
            "C_PRIVATE_CHANNEL",
            "1789344000.000100",
        )
    ]
