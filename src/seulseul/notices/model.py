"""공지 도메인 데이터 구조.

현재는 연결 테스트용으로 메모리에만 보관한다. DB 저장 모델은 DB를 정한 뒤(D-006 합의) 정의한다.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    channel_id: str
    # Slack 메시지 ts. 같은 채널 안에서 메시지를 구분하는 값이다.
    message_ts: str
    text: str
    # AI 요약. AI를 쓰지 않거나 요약에 실패하면 None이다.
    summary: str | None = None
