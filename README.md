# SeulSeul

Slack 공지를 분석해 학생별 체크리스트를 만들고, 마감일에 맞춰 DM으로 알려주는 Slack 봇입니다.

## 개발 시작

Python 3.11을 사용합니다.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
cp .env.example .env
```

## 검증

```bash
./scripts/check.sh
```

문법 검사, 테스트, Ruff 검사와 포맷 검사를 순서대로 실행합니다. 가상환경을 활성화하지 않아도 프로젝트 `.venv`를 사용합니다.

검증 전에 `.venv`를 자동으로 준비합니다. `.venv`가 없거나 Python 3.11이 아니면 다시 만들고, 개발 의존성이 없거나 `pyproject.toml`이 바뀌었으면 다시 설치합니다. Python 3.11 자체는 미리 설치되어 있어야 합니다.

Ruff 검사나 포맷 검사가 실패하면 다음 명령으로 자동 수정한 뒤 다시 검증합니다.

```bash
.venv/bin/python -m ruff check --fix .
.venv/bin/python -m ruff format .
```

## 문서

문서 목록은 `AGENTS.md`의 "먼저 읽기"를 참고합니다.
