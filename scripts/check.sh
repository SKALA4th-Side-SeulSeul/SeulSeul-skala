#!/usr/bin/env bash
# 커밋·push 전 공통 검증. 사람과 AI 에이전트가 모두 이 스크립트를 사용한다.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

# 에이전트·Git GUI 셸은 가상환경 활성화나 python alias가 없어 프로젝트 .venv를 우선 사용한다.
# .venv가 없을 때 시스템 python3(macOS 기본 3.9 등)로 넘어가면 원인 파악이 어려워 즉시 중단한다.
if [[ -z "${PYTHON:-}" ]]; then
    if [[ ! -x .venv/bin/python ]]; then
        echo ".venv가 없습니다. README.md의 '개발 시작' 명령으로 먼저 만드세요." >&2
        exit 1
    fi
    PYTHON=".venv/bin/python"
fi

echo "[1/4] Python syntax"
"${PYTHON}" -m compileall -q src tests scripts

echo "[2/4] Tests"
"${PYTHON}" -m pytest

echo "[3/4] Ruff lint"
"${PYTHON}" -m ruff check .

echo "[4/4] Ruff format"
"${PYTHON}" -m ruff format --check .

echo "All checks passed."
