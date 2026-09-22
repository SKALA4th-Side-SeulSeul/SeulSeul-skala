"""운영 안내의 수신 대상·확인·실패 처리를 실제 Slack 호출 없이 검증한다."""

from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from seulseul.checklists.model import ChecklistDeliveryError
from seulseul.database import Base
from seulseul.slack.client import SlackChecklistClient
from seulseul.users.announce import run_interactive
from seulseul.users.model import StudentModel
from seulseul.users.repository import SqlAlchemyStudentRepository


def system(answers):
    repository = Mock()
    repository.recipient_ids.return_value = ["UONE", "UTWO"]
    messenger = Mock()
    messenger.workspace_id.return_value = "TONE"
    return repository, messenger, Mock(side_effect=answers)


def test_recipients_are_current_students_in_token_workspace():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    try:
        with factory() as session, session.begin():
            for workspace, user in [("TONE", "UONE"), ("TOTHER", "UTWO")]:
                session.add(
                    StudentModel(
                        workspace_id=workspace,
                        slack_user_id=user,
                        real_name="가상 학생",
                        campus="광주",
                        class_number=1,
                    )
                )
        repository = SqlAlchemyStudentRepository(factory)
        assert repository.recipient_ids("TONE") == ["UONE"]
        repository.delete("TONE", "UONE")
        assert repository.recipient_ids("TONE") == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("confirmation", ["", "n"])
def test_no_send_without_confirmation(confirmation):
    repository, messenger, read = system(["점검 안내", EOFError, confirmation])
    assert run_interactive(repository, messenger, read=read) == 0
    messenger.send_announcement.assert_not_called()


def test_confirmed_send_skips_withdrawn_student(capsys):
    repository, messenger, read = system(["점검 안내", "잠시 기다려 주세요.", EOFError, "y"])
    repository.get.side_effect = [object(), None]
    assert run_interactive(repository, messenger, read=read, pause=Mock()) == 0
    repository.recipient_ids.assert_called_once_with("TONE")
    messenger.send_announcement.assert_called_once_with(
        "UONE", "[SeulSeul 운영 안내]\n\n점검 안내\n잠시 기다려 주세요."
    )
    assert "해지 제외 1명" in capsys.readouterr().out


@pytest.mark.parametrize("uncertain", [True, False])
def test_failure_does_not_retry_and_continues(uncertain, capsys):
    repository, messenger, read = system(["점검", EOFError, "y"])
    messenger.send_announcement.side_effect = [
        ChecklistDeliveryError("slack_connection_error", uncertain=uncertain),
        None,
    ]
    assert run_interactive(repository, messenger, read=read, pause=Mock()) == 1
    assert messenger.send_announcement.call_count == 2
    assert "성공 1명" in capsys.readouterr().out


def test_rate_limit_stops_remaining_sends(capsys):
    repository, messenger, read = system(["점검", EOFError, "y"])
    messenger.send_announcement.side_effect = ChecklistDeliveryError("ratelimited")
    assert run_interactive(repository, messenger, read=read, pause=Mock()) == 1
    assert messenger.send_announcement.call_count == 1
    assert "미발송 1명" in capsys.readouterr().out


@pytest.mark.parametrize("body", ["", "a" * 3001, "bad\x1b[31m"])
def test_invalid_input_never_sends(body):
    repository, messenger, read = system([body, EOFError, "y"])
    assert run_interactive(repository, messenger, read=read) == 1
    messenger.send_announcement.assert_not_called()


def test_announcement_uses_separate_normal_dm():
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DONE"}}
    SlackChecklistClient(client).send_announcement("UONE", "점검 안내")
    client.chat_postMessage.assert_called_once_with(
        channel="DONE", text="점검 안내", unfurl_links=False, unfurl_media=False
    )
    client.chat_update.assert_not_called()
    client.chat_delete.assert_not_called()
    client.chat_postEphemeral.assert_not_called()


def test_eof_without_body_never_sends():
    repository, messenger, _ = system([])
    assert run_interactive(repository, messenger, read=Mock(side_effect=EOFError)) == 1
    messenger.send_announcement.assert_not_called()


def test_interrupt_during_send_reports_uncertain_and_remaining(capsys):
    repository, messenger, read = system(["점검", EOFError, "y"])
    messenger.send_announcement.side_effect = KeyboardInterrupt
    assert run_interactive(repository, messenger, read=read, pause=Mock()) == 1
    output = capsys.readouterr().out
    assert "결과 불명 1명" in output and "미발송 1명" in output


@pytest.mark.parametrize("answers", [["점검", KeyboardInterrupt], ["점검", EOFError, EOFError]])
def test_cancel_during_body_or_confirmation_never_sends(answers):
    repository, messenger, read = system(answers)
    assert run_interactive(repository, messenger, read=read) == 0
    messenger.send_announcement.assert_not_called()


def test_preview_precedes_confirmation_and_preserves_paragraphs(capsys):
    repository, messenger, _ = system([])
    answers = iter(["첫 문단", "", ".done", EOFError, "y"])

    def read(prompt):
        if "발송할까요" in prompt:
            assert "첫 문단\n\n.done" in capsys.readouterr().out
            messenger.send_announcement.assert_not_called()
        value = next(answers)
        if value is EOFError:
            raise EOFError
        return value

    assert run_interactive(repository, messenger, read=read, pause=Mock()) == 0
    assert messenger.send_announcement.call_count == 2
