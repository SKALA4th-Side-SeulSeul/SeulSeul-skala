# 아키텍처 안내

## 패키지 책임

| 위치 | 책임 |
| --- | --- |
| `src/seulseul/slack/` | Slack 이벤트·상호작용 처리, 화면, Slack API 연동 |
| `src/seulseul/notices/` | 공지 메시지 수집, 채널 범위와 URL 검증, 대상 판별 |
| `src/seulseul/ai/` | AI 제공자(NVIDIA Build API 또는 Ollama)를 통한 요약·마감일 추출·검증 |
| `src/seulseul/users/` | 학생, 소속, 권한, 알림 설정 |
| `src/seulseul/checklists/` | 체크리스트 생성·배정·완료 상태 관리 |
| `src/seulseul/jobs/` | 정기 실행과 비동기 작업 |
| `src/seulseul/database.py` | PostgreSQL 연결과 SQLAlchemy 공통 세션 기반 |
| `src/seulseul/config.py` | 환경변수 로딩과 검증 |

## 경계 규칙

- 외부 API 호출은 `slack/client.py`, `ai/client.py`에만 둡니다.
- 업무 규칙은 도메인별 `service.py`가 담당합니다.
- DB 모델은 도메인별 `model.py`에 두고, 연결은 `database.py`, 도메인별 DB 조작은
  `repository.py`가 관리합니다.
- 영속 엔터티는 학생·공지·체크리스트로 분리하고, AI 처리 상태와 최근 실패 정보는 공지 모델에서 관리합니다.
- Slack 핸들러와 뷰는 입력을 검증하고 서비스를 호출한 뒤 응답만 구성합니다. DB를 직접 조작하지 않습니다.
- 정기·비동기 작업은 `jobs/tasks.py`에 구현하고 `jobs/scheduler.py`에서 등록합니다. 작업은 서비스를 호출하며 업무 규칙을 다시 구현하지 않습니다.
- 환경변수는 `config.py`에서만 읽습니다. 다른 모듈의 `os.getenv()`, `os.environ` 사용은 Ruff(`TID251`)가 차단합니다.

## 요청 흐름

```text
slack/handlers.py
  → 도메인 service.py
  → repository.py
  → model.py / database.py
  → slack/client.py 또는 ai/client.py
```

실제 구현에서 경계가 달라질 경우 코드를 억지로 맞추지 말고 `docs/DECISIONS.md`에 이유를 남깁니다.
