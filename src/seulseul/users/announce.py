"""현재 가입한 학생에게 운영 안내 DM을 수동 발송한다."""

import argparse
import sys
import time

from sqlalchemy.exc import SQLAlchemyError

from seulseul.checklists.model import ChecklistDeliveryError
from seulseul.config import ConfigError, load_database_settings, load_slack_settings
from seulseul.database import create_database_engine, create_session_factory
from seulseul.slack.client import SlackChecklistClient
from seulseul.users.repository import SqlAlchemyStudentRepository


def run_interactive(repository, messenger, *, read=input, pause=time.sleep) -> int:
    workspace = messenger.workspace_id()
    recipients = repository.recipient_ids(workspace)
    if not recipients:
        print("현재 가입한 학생이 없습니다. 발송하지 않았습니다.")
        return 0
    print("안내문을 입력하세요 (최대 3,000자).")
    print("줄바꿈: Enter · 입력 완료: 빈 줄에서 Ctrl+D · 취소: Ctrl+C")
    try:
        lines = []
        length = 0
        while True:
            try:
                line = read("안내> ")
            except EOFError:
                break
            length += len(line) + 1
            if length > 3001 or any(ord(char) < 32 and char != "\t" for char in line):
                print("안내문은 3,000자 이하이며 제어 문자가 없어야 합니다.")
                return 1
            lines.append(line)
        body = "\n".join(lines).strip()
        if not body:
            print("안내문이 비어 있어 발송하지 않았습니다.")
            return 1
        text = f"[SeulSeul 운영 안내]\n\n{body}"
        print(f"\n워크스페이스: {workspace} · 수신 대상: {len(recipients)}명\n\n{text}\n")
        if read("위 내용으로 발송할까요? [y/N]: ").strip().lower() != "y":
            print("취소했습니다. 발송하지 않았습니다.")
            return 0
    except (EOFError, KeyboardInterrupt):
        print("\n취소했습니다. 발송하지 않았습니다.")
        return 0

    sent = failed = uncertain = skipped = attempted = 0
    interrupted = False
    try:
        for index, user_id in enumerate(recipients):
            if index:
                pause(1)
            # 입력·발송을 기다리는 동안 해지한 학생은 제외한다.
            if repository.get(workspace, user_id) is None:
                skipped += 1
                continue
            attempted += 1
            try:
                messenger.send_announcement(user_id, text)
            except ChecklistDeliveryError as error:
                if error.uncertain:
                    uncertain += 1
                else:
                    failed += 1
                print(f"대상 {index + 1}: {'결과 불명' if error.uncertain else '실패'}")
                if error.code == "ratelimited":
                    print("Slack 전송 제한으로 남은 발송을 중단합니다.")
                    break
            except KeyboardInterrupt:
                uncertain += 1
                raise
            else:
                sent += 1
    except (KeyboardInterrupt, SQLAlchemyError):
        interrupted = True
        print("\n발송을 중단했습니다.")
    remaining = len(recipients) - attempted - skipped
    print(
        f"성공 {sent}명 · 실패 {failed}명 · 결과 불명 {uncertain}명 · "
        f"해지 제외 {skipped}명 · 미발송 {remaining}명"
    )
    print("자동 재시도는 하지 않습니다. 다시 실행하면 성공한 학생에게도 재발송됩니다.")
    return int(bool(failed or uncertain or remaining or interrupted))


def main(argv=None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    if not sys.stdin.isatty():
        print("터미널에서 ./announce.sh를 실행하세요. 파일·파이프 입력은 지원하지 않습니다.")
        return 1
    try:
        settings = load_slack_settings()
        engine = create_database_engine(load_database_settings())
        try:
            return run_interactive(
                SqlAlchemyStudentRepository(create_session_factory(engine)),
                SlackChecklistClient.from_token(settings.bot_token),
            )
        finally:
            engine.dispose()
    except (ConfigError, SQLAlchemyError, ChecklistDeliveryError):
        print("발송 준비에 실패했습니다. DB 연결과 Slack 봇 설정을 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
