# SeulSeul 작업 지도

이 문서는 사용하는 AI 도구와 관계없이 적용됩니다. Codex는 이 파일을 직접 읽고, Claude Code는 `CLAUDE.md`를 통해 읽습니다. 개인 전역 설정과 충돌하면 이 문서와 아래 기준 문서를 우선합니다.

## 프로젝트 개요

- 목적: Slack 공지를 분석해 학생별 체크리스트를 만들고, 완료 여부를 관리하며 DM으로 알리는 Slack 봇
- 언어: Python 3.11
- 학생 소속: Slack 성명(`profile.real_name`)의 `4기_광주_<1~4>반_<이름>`으로 판별 (D-021)
- 실패 공지 복구: 운영자용 `python -m seulseul.notices.retry` CLI (사용법은 `README.md`, 정책은 D-022)
- 원문 동기화: 수정·링크 추가/제거·삭제와 완료 상태 보존, `notice_sources`로 중복·역순 이벤트 방어 (D-024)
- 학생 화면: 시작·해지 명령만 사용, 최초 연결 시 개인 DM 한 번 발송 후 같은 메시지 갱신 (D-025), 테두리 카드 묶음·굵은 짧은 제목 링크·📝 폼/📄 문서·작은 도메인/마감/원문·✓ 완료/↶ 취소, 하단 단일 목록 전환·새로고침·페이지 이동
- 용어: `docs/PRODUCT.md`의 "용어"
- 운영 백업: `./backup.sh`로 실행 중인 운영 DB를 저장소 상위 backups에 백업합니다. `./backup_run.sh`·`./backup_stop.sh`로 매일 한국 시간 03시 systemd 사용자 예약을 등록·해제합니다(lingering 필요). 기존 백업은 삭제하지 않으며 복구 시험·외부 보관은 별도입니다.
- 다중 채널 동일 링크(D-027): 공지는 원본별 저장, 체크리스트는 학생·링크당 하나. 유효한 반 원본 우선·동일 우선순위 최신 게시 원본 선택, 완료 기록 보존. 실패 재처리는 원본 채널·ts로 구분합니다.
- 운영 DB 외부 조회: `docs/DB-ACCESS.md`. 서버 `127.0.0.1:5432` 바인딩만 허용하고 별도 키 인증 SSH 터널 계정과 조회 전용 DB 계정을 사용합니다. 배포 계정 포워딩 금지는 유지합니다.
- 운영 스크립트: 루트 `run.sh`·`stop.sh`·`view.sh`는 운영 Compose·Rootless Docker 전용입니다. DB 볼륨은 삭제하지 않습니다. 사용법은 README의 운영 서버 간편 명령을 따릅니다.
- 슬래시 명령은 빈 ack만 전송하고 임시 메시지를 보내지 않습니다. 오류는 로그에 기록하며 버튼 오류 안내는 유지합니다.
- 해지 완료(미가입 포함) 안내는 일반 DM으로 전송합니다. 시작은 체크리스트만 보냅니다. 봇은 최신 코드의 단일 프로세스로 실행합니다.
- 목록 상단: `미완료/완료 목록: N개 · 마지막 갱신 MM/DD(한국어 요일)`. 하단은 페이지 표시만 유지합니다.
- 시작·해지의 기존 봇 개인 DM 정리: D-026. 시작 시 완료 기록 보존, 실패 시 갱신 중지 후 명령 재시도. `im:history` 권한 필요.

## 디렉터리 구조

```text
src/seulseul/
├── slack/       # 이벤트·화면·Slack API. 업무 규칙과 DB 조작 금지
├── notices/     # 공지 수집, 채널·URL 검증, 대상 판별
├── ai/          # AI(NVIDIA API·Ollama) 요약·마감일 추출. 외부 호출은 client.py에만
├── users/       # 학생·소속·권한·알림 설정
├── checklists/  # 체크리스트 생성·배정·완료 상태
├── jobs/        # 정기 작업. 서비스 호출만 하고 업무 규칙 재구현 금지
├── config.py    # 환경변수를 읽는 유일한 위치
└── database.py  # DB 연결
tests/test_<domain>.py  # 도메인별 테스트
```

상세 경계와 요청 흐름은 `docs/ARCHITECTURE.md`를 따릅니다.

## 절대 금지

- `config.py` 밖에서 `os.getenv()`, `os.environ` 사용 (Ruff `TID251`이 차단)
- `slack/client.py`, `ai/client.py` 밖에서 외부 API 호출 (명령 응답 `respond()` 포함)
- Slack 핸들러·뷰에서 DB 직접 조작
- 테스트에서 실제 Slack·AI(NVIDIA·Ollama) API 호출
- `.env`, 토큰, 실제 사용자 정보 커밋
- `main` 외 브랜치 생성, 사용자 요청 없는 커밋·push
- `docs/DECISIONS.md` D-006의 합의 필요 파일을 사용자 확인 없이 수정
- `docs/DECISIONS.md`의 "포기한 대안"을 사용자 요청 없이 다시 제안

## 정리 규칙

- 작업 중 만든 임시 파일과 디버그 코드는 완료 전에 삭제합니다.
- `temp_`, `_new`, `_old`, `_backup`이 들어간 파일 이름을 만들지 않습니다.
- 사용하지 않는 import와 함수는 남기지 않습니다.
- 코드 변경으로 문서 내용이 달라지면 같은 작업에서 기준 문서를 갱신합니다. 이 문서의 요약도 함께 맞춥니다.

## 커밋과 테스트

- 커밋 메시지: `<타입>: <한국어 설명>` (`feat`, `fix`, `refactor`, `test`, `docs`, `chore`, 상세는 D-005)
- 동작을 추가하거나 바꾸면 `tests/test_<domain>.py`에 테스트를 추가합니다.
- 작업을 마치면 `docs/PLANS.md`의 해당 항목을 갱신합니다.

## 기준 문서

- 설치와 검증: `README.md`
- 브랜치·커밋·합의 필요 파일·포기한 대안: `docs/DECISIONS.md`
- 환경변수·코드 규칙과 작업 흐름: `COLLABORATION.md`
- 코드 경계: `docs/ARCHITECTURE.md`
- 제품 흐름과 용어: `docs/PRODUCT.md`
- 현재 작업: `docs/PLANS.md`
- 로컬·서버(Docker, PostgreSQL, Oracle Cloud) 작업: `docs/DB-TODO.md`

## 완료 기준

`./scripts/check.sh`가 통과해야 완료입니다. 이 스크립트는 프로젝트 `.venv`를 Python 3.11로 준비하고 개발 의존성을 자동으로 맞춥니다. `No module named ...` 오류가 나도 `.venv`를 직접 고치지 말고 이 스크립트를 다시 실행합니다.
