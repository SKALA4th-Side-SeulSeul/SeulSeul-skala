"""운영자 수동 공지 CLI의 입력 검증과 이벤트 변환을 확인한다."""

from dataclasses import replace
from datetime import datetime
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest

from seulseul.ai.model import NoticeAnalysis
from seulseul.notices.manual import (
    build_manual_event,
    parse_manual_analysis,
    parse_slack_permalink,
)
from seulseul.notices.repository import InMemoryNoticeRepository
from seulseul.notices.service import NoticeService

SEOUL = ZoneInfo("Asia/Seoul")
SOURCE_URL = "https://workspace.slack.com/archives/C123ABC4567/p1789559318987269"
MESSAGE_TS = "1789559318.987269"


def test_parse_slack_permalink_extracts_channel_and_message_ts() -> None:
    assert parse_slack_permalink(SOURCE_URL) == ("C123ABC4567", MESSAGE_TS)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/archives/C123ABC4567/p1789559318987269",
        "https://workspace.slack.com/archives/C123ABC4567/not-a-message",
        "https://workspace.slack.com/archives/U123ABC4567/p1789559318987269",
    ],
)
def test_parse_slack_permalink_rejects_non_slack_message_urls(url: str) -> None:
    with pytest.raises(ValueError, match="Slack 원문 링크"):
        parse_slack_permalink(url)


def test_build_manual_event_uses_created_or_changed_identity() -> None:
    created = build_manual_event("C123ABC4567", MESSAGE_TS, "공지", kind="created")
    changed = build_manual_event("C123ABC4567", MESSAGE_TS, "수정", kind="changed")

    assert created["ts"] == MESSAGE_TS
    assert changed["message"]["ts"] == MESSAGE_TS
    assert changed["message"]["edited"]["ts"] > MESSAGE_TS


def test_parse_manual_analysis_assumes_seoul_when_offset_is_omitted() -> None:
    analysis = parse_manual_analysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline="2026-09-30 23:59",
        deadline_source_text="9월 30일 23:59",
    )

    assert analysis == NoticeAnalysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline_at=datetime(2026, 9, 30, 23, 59, tzinfo=SEOUL),
        deadline_source_text="9월 30일 23:59",
    )


def test_manual_events_register_update_and_delete_same_source() -> None:
    service = NoticeService(
        {"C123ABC4567"},
        repository=InMemoryNoticeRepository(10),
    )
    analysis = parse_manual_analysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline="2026-09-30 23:59",
        deadline_source_text="9월 30일 23:59",
    )
    created = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, "https://forms.example/task", kind="created"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        manual_analysis=analysis,
    )
    changed = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, "https://forms.example/task", kind="changed"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        manual_analysis=parse_manual_analysis(
            title="수정 설문",
            summary="수정된 설문을 제출합니다.",
            deadline="2026-10-01 23:59",
            deadline_source_text="10월 1일 23:59",
        ),
    )
    deleted = service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, kind="deleted"),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
    )

    assert created[0].analysis == analysis
    assert changed[0].analysis is not None and changed[0].analysis.title == "수정 설문"
    assert deleted[0].deleted_at is not None


@pytest.fixture
def editor_system():
    repository = InMemoryNoticeRepository(100)
    analyzer = Mock(side_effect=AssertionError("AI 호출 금지"))
    service = NoticeService({"C123ABC4567"}, repository=repository, analyzer=analyzer)
    analysis = parse_manual_analysis(
        title="설문",
        summary="설문을 제출합니다.",
        deadline="2026-09-30 23:59",
        deadline_source_text="9월 30일 23:59",
    )
    original = service.record_channel_message(
        build_manual_event(
            "C123ABC4567",
            MESSAGE_TS,
            "9월 30일 23:59까지 https://forms.example/task",
            kind="created",
        ),
        workspace_id="T123ABC4567",
        source_permalink=SOURCE_URL,
        manual_analysis=analysis,
    )[0]
    return service, repository, original, analyzer


def test_interactive_edit_loads_identity_and_keeps_blank_fields(editor_system, capsys):
    from seulseul.notices.manual_edit import run_interactive

    service, repository, original, analyzer = editor_system
    answers = iter(["1", "변경 제목", "", "", "", "y"])
    assert run_interactive(service, read=lambda prompt: next(answers)) == 0
    updated = repository.recent(1)[0]
    assert updated == replace(original, analysis=replace(original.analysis, title="변경 제목"))
    analyzer.analyze.assert_not_called()
    output = capsys.readouterr().out
    assert "변경 제목" in output and SOURCE_URL in output and "저장했습니다" in output


@pytest.mark.parametrize("answers", [["q"], ["1", "수정", "", "", "", ""], ["1", "", "", "", ""]])
def test_interactive_cancel_or_no_change_never_writes(editor_system, answers, monkeypatch):
    from seulseul.notices.manual_edit import run_interactive

    service, _, _, _ = editor_system
    save = Mock(side_effect=AssertionError("취소 시 저장 금지"))
    monkeypatch.setattr(service, "record_channel_message", save)
    inputs = iter(answers)
    assert run_interactive(service, read=lambda prompt: next(inputs)) == 0
    save.assert_not_called()


@pytest.mark.parametrize("interrupt", [EOFError, KeyboardInterrupt])
def test_interactive_interruption_preserves_notice(editor_system, interrupt):
    from seulseul.notices.manual_edit import run_interactive

    service, repository, original, _ = editor_system

    def read(prompt):
        raise interrupt

    assert run_interactive(service, read=read) == 0
    assert repository.recent(1) == [original]


def test_interactive_invalid_inputs_retry_only_the_invalid_field(editor_system, capsys):
    from seulseul.notices.manual_edit import run_interactive

    service, repository, _, _ = editor_system
    answers = iter(["100", "no", "1", "x" * 256, "수정", "", "9/99", "2026-10-01 18:00", "", "y"])
    assert run_interactive(service, read=lambda prompt: next(answers)) == 0
    analysis = repository.recent(1)[0].analysis
    assert analysis.title == "수정"
    assert analysis.deadline_at == datetime(2026, 10, 1, 18, tzinfo=SEOUL)
    assert "YYYY-MM-DD" in capsys.readouterr().out


def test_interactive_concurrent_change_is_not_overwritten(editor_system, capsys):
    from seulseul.notices.manual_edit import run_interactive

    service, repository, original, _ = editor_system
    answers = iter(["1", "운영자 수정", "", "", "", "y"])

    def read(prompt):
        answer = next(answers)
        if answer == "y":
            service.record_channel_message(
                build_manual_event(
                    original.channel_id,
                    original.message_ts,
                    "변경 https://forms.example/task",
                    kind="changed",
                ),
                workspace_id=original.workspace_id,
                source_permalink=SOURCE_URL,
                manual_analysis=replace(original.analysis, title="다른 운영자 수정"),
            )
        return answer

    assert run_interactive(service, read=read) == 1
    assert repository.recent(1)[0].analysis.title == "다른 운영자 수정"
    assert "변경" in capsys.readouterr().out


def test_interactive_multiple_links_are_rejected_without_truncating_source(editor_system):
    from seulseul.notices.manual_edit import run_interactive

    service, repository, original, analyzer = editor_system
    analyzer.analyze.return_value = original.analysis
    service.record_channel_message(
        build_manual_event(
            original.channel_id,
            original.message_ts,
            original.text + " https://docs.example/second",
            kind="changed",
        ),
        workspace_id=original.workspace_id,
        source_permalink=SOURCE_URL,
    )
    before = repository.recent(10)
    answers = iter(["1"])
    assert run_interactive(service, read=lambda prompt: next(answers)) == 1
    assert repository.recent(10) == before


def test_interactive_empty_list_needs_no_input(capsys):
    from seulseul.notices.manual_edit import run_interactive

    service = NoticeService({"C123ABC4567"})
    assert run_interactive(service, read=Mock(side_effect=AssertionError("입력 불필요"))) == 0
    assert "없습니다" in capsys.readouterr().out


@pytest.mark.parametrize("missing_permalink", [False, True])
def test_interactive_recovers_failed_notice_without_analysis(missing_permalink):
    from seulseul.notices.manual_edit import run_interactive

    service = NoticeService({"C123ABC4567"})
    service.record_channel_message(
        build_manual_event("C123ABC4567", MESSAGE_TS, "https://forms.example/task", kind="created"),
        workspace_id="T123ABC4567",
        source_permalink="" if missing_permalink else SOURCE_URL,
        collection_error="분석 실패",
    )
    entries = ["1"]
    if missing_permalink:
        # 제출 URL과 다른 Slack 원문은 거절하고 선택한 원문만 허용한다.
        entries += [
            "https://forms.example/task",
            SOURCE_URL.replace("C123ABC4567", "COTHER"),
            SOURCE_URL,
        ]
    entries += ["", "제목", "요약", "2026-10-01 18:00", "", "y"]
    answers = iter(entries)
    assert run_interactive(service, read=lambda prompt: next(answers)) == 0
    notice = service.recent_notices(1)[0]
    assert notice.processing_status == "processed" and notice.last_error is None
    assert notice.analysis.title == "제목" and notice.source_permalink == SOURCE_URL


def test_interactive_interrupt_during_save_does_not_claim_nothing_was_saved(
    editor_system, monkeypatch, capsys
):
    from seulseul.notices.manual_edit import run_interactive

    service, _, _, _ = editor_system
    monkeypatch.setattr(service, "record_channel_message", Mock(side_effect=KeyboardInterrupt))
    answers = iter(["1", "수정", "", "", "", "y"])
    assert run_interactive(service, read=lambda prompt: next(answers)) == 1
    output = capsys.readouterr().out
    assert "결과를 확인" in output and "저장하지 않았습니다" not in output


def test_interactive_terminal_escape_is_not_printed(editor_system, capsys):
    from seulseul.notices.manual_edit import run_interactive

    service, _, _, _ = editor_system
    answers = iter(["1", "제목\x1b[2J", "", "", "", "n"])
    assert run_interactive(service, read=lambda prompt: next(answers)) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output and "\\u001b" in output


@pytest.mark.parametrize("argument", ["0", "101", "abc"])
def test_interactive_cli_rejects_invalid_limit_before_loading_settings(argument, monkeypatch):
    from seulseul.notices import manual_edit

    load = Mock(side_effect=AssertionError("설정 조회 불필요"))
    monkeypatch.setattr(manual_edit, "load_slack_settings", load)
    with pytest.raises(SystemExit) as error:
        manual_edit.main(["--limit", argument])
    assert error.value.code == 2
    load.assert_not_called()
