# 관리자 공지 분석 DM 구현 계획

Spec: `docs/superpowers/specs/2026-10-26-admin-notice-monitoring-design.md`

## 파일과 경계

- `src/seulseul/notices/service.py`: 저장 확정 결과 알림 콜백. Slack 호출 없음.
- `src/seulseul/slack/views.py`: 성공·실패 Block Kit.
- `src/seulseul/slack/client.py`: 관리자별 DM 발송·닫기 API.
- `src/seulseul/slack/handlers.py`: 닫기 버튼 처리·관리자와 DM 확인.
- `src/seulseul/main.py`, `src/seulseul/notices/retry.py`, `src/seulseul/notices/manual.py`, `src/seulseul/notices/manual_console.py`: AI 분석 실행 지점에 알림 연결.
- `tests/test_notices.py`, `tests/test_slack.py`: 분석 저장·중복·재처리·부분 발송 실패·닫기 검증.
- `docs/PLANS.md`, `AGENTS.md`: 구현 완료와 운영 검증 경계 기록.

## Task 1: 저장 확정 후 결과 통지

- [x] 결과 콜백을 받는 `NoticeService` 테스트: 신규 성공·실패, 역순/중복 무발송, 수동 분석 무발송, 수동·자동 재처리 결과, 콜백 실패 때 공지 저장 유지.
- [x] 실패를 확인한 뒤 `on_result: Callable[[Notice], None]`을 추가하고 저장소의 실제 변경 결과에만 호출한다.
- [x] 해당 테스트 통과 확인.

## Task 2: DM 화면과 닫기

- [x] 두 관리자에게만 각각 일반 DM을 보내는 테스트, 성공·실패 화면과 닫기 버튼 테스트, 권한·DM 채널·시각 검증 및 삭제 실패 안내 테스트 작성 후 실패 확인.
- [x] `slack/views.py`, `slack/client.py`, `slack/handlers.py`에 구현. 전송 실패는 관리자별로 로그에 남기며 다음 관리자 발송을 계속한다.
- [x] 해당 테스트 통과 확인.

## Task 3: 실행 지점 연결과 문서

- [x] 봇 초기화와 운영자 AI 명령이 같은 알림 콜백을 쓰도록 연결하고 diff에서 확인.
- [x] 각 서비스 생성 위치에 콜백 주입. AI 없는 수동 분석은 알림을 보내지 않는다.
- [x] 기준 문서와 `docs/PLANS.md` 갱신.
- [x] `./scripts/check.sh` 전체 통과 및 diff 검토.

## Review Focus

- 중복·역순 이벤트가 새 DM을 만들지 않는지.
- 자동 재처리 예약 단계와 완료 결과의 DM 수가 일치하는지.
- 한 관리자 발송 오류가 다른 관리자 발송과 공지 저장에 영향을 주지 않는지.
- 닫기 요청의 사용자·DM 채널·메시지 시각이 확인되는지.
