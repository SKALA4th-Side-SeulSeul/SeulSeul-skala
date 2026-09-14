# SeulSeul 작업 지도

SeulSeul은 Slack 공지를 분석해 학생별 체크리스트를 만들고, 정기적으로 DM 알림을 보내는 작은 Slack 봇입니다.

이 문서는 사용하는 AI 도구와 관계없이 적용됩니다. Codex는 이 파일을 직접 읽고, Claude Code는 `CLAUDE.md`를 통해 읽습니다. 개인 전역 설정과 충돌하면 이 문서와 아래 문서를 우선합니다.

## 먼저 읽기

- 설치와 검증 명령: `README.md`
- 브랜치·커밋·합의가 필요한 파일: `docs/DECISIONS.md` (`COLLABORATION.md`와 다르면 이 문서를 따릅니다)
- 협업·코드 규칙: `COLLABORATION.md`
- 패키지 책임과 코드 경계: `docs/ARCHITECTURE.md`
- 제품 흐름: `docs/PRODUCT.md`
- 현재 작업: `docs/PLANS.md`

## 에이전트 규칙

- `docs/DECISIONS.md` D-006의 합의가 필요한 파일은 수정 전에 사용자에게 확인합니다.
- 커밋과 push는 사용자가 요청할 때만 합니다.
- 작업을 마치면 `docs/PLANS.md`의 해당 항목을 갱신합니다.
- 규칙을 바꿀 때는 기준 문서 한 곳만 수정하고, 다른 문서에 내용을 복사하지 않습니다.
- 도구 전용 규칙이 필요하면 이 문서가 아니라 해당 도구 파일(`CLAUDE.md` 등)에 추가합니다.

## 완료 기준

`./scripts/check.sh`가 통과해야 완료입니다. 이 스크립트는 가상환경 활성화 없이 프로젝트 `.venv`를 사용하며, `.venv`가 없으면 `README.md`의 "개발 시작" 명령을 먼저 실행합니다.
