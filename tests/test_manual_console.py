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


def test_console_edit_choice_forwards_limit_and_workspace(monkeypatch):
    # 수정 전용 notice_edit.sh를 없앤 대신 메뉴 2번이 같은 수정 화면을 같은 범위로 열어야 한다.
    from seulseul.notices import manual_console

    captured = {}

    def fake_edit(service, *, limit, workspace_id, read):
        captured.update(service=service, limit=limit, workspace_id=workspace_id)
        return 0

    monkeypatch.setattr(manual_console, "edit", fake_edit)
    service = NoticeService({"C123ABC4567"}, repository=InMemoryNoticeRepository(20))

    result = manual_console.run_interactive(
        service,
        allowed_channels={"C123ABC4567"},
        read=lambda _: "2",
        workspace_id="T123ABC4567",
        limit=100,
    )

    assert result == 0
    assert captured == {"service": service, "limit": 100, "workspace_id": "T123ABC4567"}


UNCONFIGURED_URL = "https://workspace.slack.com/archives/C0NOBOT0001/p1789559318987269"
TARGETS = {"C123ABC4567": None, "C2CLASS0002": 2}


def _manual_answers(*head):
    return iter(
        [
            *head,
            "공지 https://forms.gle/example",
            ".done",
            "n",
            "설문 제출",
            "설문에 참여해 주세요.",
            "2026-09-30 23:59",
            "9월 30일 23:59까지",
            "y",
        ]
    )


def test_console_registers_notice_from_channel_without_bot_under_chosen_target(capsys):
    # 봇이 없는 채널의 원문도 등록하고, 고른 배정 기준 채널의 대상 학생에게 보이게 한다.
    from seulseul.notices.manual_console import run_interactive

    channels = {"C123ABC4567", "C2CLASS0002"}
    service = NoticeService(channels, repository=InMemoryNoticeRepository(20))
    answers = _manual_answers("1", UNCONFIGURED_URL, "9", "2")

    result = run_interactive(
        service,
        allowed_channels=("C123ABC4567", "C2CLASS0002"),
        read=lambda _: next(answers),
        channel_targets=TARGETS,
        workspace_resolver=lambda: "T123ABC4567",
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "봇이 없거나 설정에 없는 채널" in output
    assert "1) 광주 전체 · C123ABC4567" in output and "2) 2반 · C2CLASS0002" in output
    assert "1~2 사이의 번호" in output
    assert "워크스페이스 T123ABC4567 (봇 토큰 기준 자동 확인)" in output
    notice = service.recent_notices(1, configured_only=True)[0]
    assert notice.workspace_id == "T123ABC4567"
    assert notice.channel_id == "C2CLASS0002"
    assert notice.message_ts == "1789559318.987269"
    assert notice.source_permalink == UNCONFIGURED_URL
    assert notice.processing_status == "processed"


def test_console_uses_configured_source_channel_without_asking(capsys):
    from seulseul.notices.manual_console import run_interactive

    service = NoticeService({"C123ABC4567"}, repository=InMemoryNoticeRepository(20))
    answers = _manual_answers("1", SOURCE_URL)

    assert (
        run_interactive(
            service,
            allowed_channels=("C123ABC4567",),
            read=lambda _: next(answers),
            channel_targets=TARGETS,
            workspace_resolver=lambda: "T123ABC4567",
        )
        == 0
    )
    assert "배정 대상: 광주 전체 (원문 채널 설정)" in capsys.readouterr().out
    assert service.recent_notices(1)[0].channel_id == "C123ABC4567"


def test_console_asks_workspace_only_when_automatic_lookup_fails(capsys):
    from seulseul.checklists.model import ChecklistDeliveryError
    from seulseul.notices.manual_console import run_interactive

    def unavailable():
        raise ChecklistDeliveryError("slack_connection_error")

    service = NoticeService({"C123ABC4567"}, repository=InMemoryNoticeRepository(20))
    answers = _manual_answers("1", "T123ABC4567", SOURCE_URL)

    assert (
        run_interactive(
            service,
            allowed_channels=("C123ABC4567",),
            read=lambda _: next(answers),
            workspace_resolver=unavailable,
        )
        == 0
    )
    assert "자동으로 확인하지 못했습니다(slack_connection_error)" in capsys.readouterr().out
    assert service.recent_notices(1)[0].workspace_id == "T123ABC4567"


def test_console_target_choice_can_be_cancelled_without_saving():
    from seulseul.notices.manual_console import run_interactive

    repository = InMemoryNoticeRepository(20)
    service = NoticeService({"C123ABC4567"}, repository=repository)
    answers = iter(["1", UNCONFIGURED_URL, "q"])

    assert (
        run_interactive(
            service,
            allowed_channels=("C123ABC4567",),
            read=lambda _: next(answers),
            workspace_resolver=lambda: "T123ABC4567",
        )
        == 0
    )
    assert service.recent_notices(10) == []


def _patch_console_dependencies(monkeypatch, manual_console):
    class FakeSettings:
        bot_token = "xoxb-test"
        notice_channels = ("C123ABC4567",)
        manual_notice_channels = ()

    class FakeEngine:
        def dispose(self):
            pass

    monkeypatch.setattr(manual_console, "configure_logging", lambda: None)
    monkeypatch.setattr(manual_console, "load_slack_settings", lambda: FakeSettings())
    monkeypatch.setattr(manual_console, "load_notice_targets", lambda *a, **k: TARGETS)
    monkeypatch.setattr(manual_console, "load_database_settings", lambda: object())
    monkeypatch.setattr(manual_console, "create_database_engine", lambda settings: FakeEngine())
    monkeypatch.setattr(manual_console, "create_session_factory", lambda engine: object())
    monkeypatch.setattr(
        manual_console, "SqlAlchemyNoticeRepository", lambda factory: InMemoryNoticeRepository(20)
    )


def test_console_failure_shows_stage_cause_hint_and_logs_it(monkeypatch, capsys, caplog):
    from seulseul.notices import manual_console

    _patch_console_dependencies(monkeypatch, manual_console)
    answers = iter(["1", "https://example.com/not-slack"])
    original = manual_console.run_interactive
    monkeypatch.setattr(
        manual_console,
        "run_interactive",
        lambda *args, **kwargs: original(*args, read=lambda _: next(answers), **kwargs),
    )
    monkeypatch.setattr(
        manual_console.SlackChecklistClient,
        "from_token",
        classmethod(lambda cls, token: type("C", (), {"workspace_id": lambda self: "T1"})()),
    )

    with caplog.at_level("INFO", logger="seulseul.notices.manual_console"):
        assert manual_console.main([]) == 1

    output = capsys.readouterr().out
    assert "✖ 수동 공지 처리에 실패했습니다." in output
    assert "작업  등록" in output and "단계  원문 링크 확인" in output
    assert "원인  Slack 원문 링크 형식이 올바르지 않습니다." in output
    assert "링크 복사" in output and "./view.sh logs manual" in output
    assert any(
        "수동 공지 처리 실패: action=등록 stage=원문 링크 확인 error=ValueError" in record.message
        for record in caplog.records
    )


def test_console_failure_masks_secrets_and_hides_database_details(monkeypatch, capsys, caplog):
    from sqlalchemy.exc import OperationalError

    from seulseul.notices import manual_console

    _patch_console_dependencies(monkeypatch, manual_console)

    def fail_with_secret(*args, **kwargs):
        raise ValueError("토큰 xoxb-1234-secret 와 https://hooks.example.test/private 실패")

    monkeypatch.setattr(manual_console, "run_interactive", fail_with_secret)
    with caplog.at_level("ERROR", logger="seulseul.notices.manual_console"):
        assert manual_console.main([]) == 1
    masked = capsys.readouterr().out + caplog.text
    assert "xoxb-1234-secret" not in masked and "hooks.example.test" not in masked
    assert "[숨김]" in masked

    def fail_database(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("postgresql://user:pw@db/app"))

    monkeypatch.setattr(manual_console, "run_interactive", fail_database)
    assert manual_console.main([]) == 1
    output = capsys.readouterr().out
    assert "DB 작업에 실패했습니다(OperationalError)." in output
    assert "user:pw" not in output + caplog.text
