# SeulSeul 협업 가이드

두 개발자가 함께 지키는 환경변수·코드 규칙과 작업 흐름입니다. 다른 규칙은 아래 기준 문서를 따르며, 이 문서에 반복해 적지 않습니다.

| 내용 | 기준 문서 |
| --- | --- |
| 설치와 검증 명령 | `README.md` |
| 브랜치, 커밋 메시지, 합의가 필요한 파일 | `docs/DECISIONS.md` (D-003, D-005, D-006) |
| 패키지 책임과 코드 경계 | `docs/ARCHITECTURE.md` |
| 제품 흐름과 용어 | `docs/PRODUCT.md` |

## 환경변수

`.env.example`을 복사해 개인 `.env`를 만들고 값을 채웁니다. `.env`와 토큰, 비밀번호, 실제 사용자 정보는 Git에 올리지 않습니다. 새 환경변수는 `.env.example`에 키 이름만 추가합니다.

| 영역 | 접두사 | 예시 |
| --- | --- | --- |
| Slack | `SLACK_` | `SLACK_BOT_TOKEN` |
| 데이터베이스 | `DATABASE_` | `DATABASE_URL` |
| AI 공통 | `AI_` | `AI_PROVIDER` |
| NVIDIA Build API | `NVIDIA_` | `NVIDIA_API_KEY` |
| Ollama 로컬 LLM | `OLLAMA_` | `OLLAMA_MODEL` |
| 앱 설정 | `APP_` | `APP_ENV` |

환경변수를 읽는 위치는 `docs/ARCHITECTURE.md`의 경계 규칙을 따릅니다.

## 코드 규칙

- Python 3.11 문법과 타입 힌트를 사용합니다.
- 함수와 변수는 영어 `snake_case`, 클래스는 `PascalCase`, 상수는 `UPPER_SNAKE_CASE`를 사용합니다.
- 새 동작을 추가하거나 수정할 때는 같은 범위의 테스트를 `tests/test_<domain>.py`에 추가합니다.

## 작업 시작 전 확인

1. `docs/DECISIONS.md` D-003의 절차대로 최신 변경을 받습니다.
2. `.env`가 로컬에 준비됐는지 확인합니다.
3. 변경할 도메인 패키지와 관련 테스트를 먼저 확인합니다.

## 결정이 필요한 경우

API 계약, DB 스키마, 권한 정책, Slack 화면 흐름처럼 다른 도메인에 영향을 주는 변경은 구현 전에 팀과 합의하고 `docs/DECISIONS.md`에 기록합니다.
