# SeulSeul 협업 가이드

## 프로젝트 개요

SeulSeul은 Slack 공지를 분석해 학생별 체크리스트를 만들고, 정기적으로 DM 알림을 보내는 서비스입니다.

코드는 `src/seulseul` 아래에 두며, 기능별 책임은 다음과 같습니다.

| 패키지 | 책임 |
| --- | --- |
| `slack` | Slack 이벤트·상호작용 처리와 화면, API 호출 |
| `notices` | 공지와 채널 범위 관리, URL 검사와 대상 판별 |
| `ai` | NVIDIA Build API를 통한 요약·마감일 추출·검증 |
| `users` | 학생, 소속, 권한, 알림 설정 |
| `checklists` | 체크리스트 생성·배정·완료 상태 관리 |
| `jobs` | 정기 작업과 비동기 처리 |

## 개발 환경

Python 3.11을 기준으로 개발합니다. 가상환경은 프로젝트 루트의 `.venv`를 사용합니다.

```bash
source .venv/bin/activate
python --version
```

의존성을 정의한 뒤에는 다음 명령으로 설치합니다.

```bash
pip install -e '.[dev]'
```

테스트는 프로젝트 루트에서 실행합니다.

```bash
pytest
```

## 환경변수

`.env.example`을 복사해 개인 `.env` 파일을 만들고 값을 채웁니다. `.env`와 토큰, 비밀번호, 실제 사용자 정보는 Git에 올리지 않습니다.

```bash
cp .env.example .env
```

프로젝트에서 사용할 환경변수의 이름은 다음 형식을 따릅니다.

| 영역 | 접두사 | 예시 |
| --- | --- | --- |
| Slack | `SLACK_` | `SLACK_BOT_TOKEN` |
| 데이터베이스 | `DATABASE_` | `DATABASE_URL` |
| AI | `NVIDIA_` | `NVIDIA_API_KEY` |
| 앱 설정 | `APP_` | `APP_ENV` |

환경변수는 `config.py`에서 한 번만 읽고 검증합니다. 다른 모듈에서 `os.getenv()`를 직접 호출하지 않습니다.

## 코드 규칙

- Python 3.11 문법과 타입 힌트를 사용합니다.
- 외부 API 호출은 `slack/client.py`, `ai/client.py`에만 둡니다. 업무 규칙은 각 도메인의 `service.py`에 둡니다.
- DB 모델은 각 도메인의 `model.py`에 둡니다. Slack 핸들러와 뷰에서 DB를 직접 조작하지 않습니다.
- Slack 요청 처리는 얇게 유지합니다. 입력을 검증한 뒤 서비스 메서드를 호출하고 응답만 구성합니다.
- 비동기·정기 작업은 `jobs/tasks.py`에 구현하고 `jobs/scheduler.py`에서 등록합니다.
- 새 동작을 추가하거나 수정할 때는 같은 범위의 테스트를 `tests/test_<domain>.py`에 추가합니다.
- 함수와 변수는 영어 `snake_case`, 클래스는 `PascalCase`, 상수는 `UPPER_SNAKE_CASE`를 사용합니다.

## 브랜치와 변경 사항

- 작업은 `main`에서 분기한 짧은 기능 브랜치에서 진행합니다. 예: `feature/notice-parser`, `fix/deadline-timezone`
- 한 PR에는 하나의 목적만 담습니다.
- 커밋 제목은 동사로 시작하고 변경 목적을 분명히 씁니다. 예: `Add checklist assignment service`
- PR에는 변경 목적, 주요 변경점, 실행한 테스트를 적습니다.
- 리뷰 전에는 테스트를 실행하고, 환경변수나 비밀값이 포함되지 않았는지 확인합니다.

## 작업 시작 전 확인

1. 최신 `main` 기준으로 브랜치를 만듭니다.
2. `.venv`를 활성화하고 의존성을 설치합니다.
3. `.env`가 로컬에 준비됐는지 확인합니다.
4. 변경할 도메인 패키지와 관련 테스트를 먼저 확인합니다.

## 결정이 필요한 경우

API 계약, DB 스키마, 권한 정책, Slack 화면 흐름처럼 다른 도메인에 영향을 주는 변경은 구현 전에 팀과 합의하고 PR 설명에 결정 내용을 남깁니다.
