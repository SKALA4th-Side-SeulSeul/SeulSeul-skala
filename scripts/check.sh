#!/usr/bin/env bash
# 커밋·push 전 공통 검증. 사람과 AI 에이전트가 모두 이 스크립트를 사용한다.
# PYTHON을 지정하지 않으면 프로젝트 .venv를 Python 3.11로 준비하고 개발 의존성을 맞춘 뒤 검증한다.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

REQUIRED_PYTHON="3.11"
VENV_DIR=".venv"
# pyproject.toml이 바뀌면 의존성을 다시 설치하기 위해 마지막 설치 시점의 해시를 .venv 안에 남긴다.
DEPS_STAMP="${VENV_DIR}/.seulseul-deps-hash"

venv_python() {
    # Windows(Git Bash)의 venv는 실행 파일을 Scripts/python.exe에 둔다.
    if [[ -x "${VENV_DIR}/bin/python" ]]; then
        echo "${VENV_DIR}/bin/python"
    elif [[ -x "${VENV_DIR}/Scripts/python.exe" ]]; then
        echo "${VENV_DIR}/Scripts/python.exe"
    fi
}

python_version() {
    "$@" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || true
}

find_base_python() {
    local candidate
    for candidate in "python${REQUIRED_PYTHON}" python3 python; do
        if command -v "${candidate}" >/dev/null 2>&1 \
            && [[ "$(python_version "${candidate}")" == "${REQUIRED_PYTHON}" ]]; then
            echo "${candidate}"
            return
        fi
    done
    # Windows Python 런처
    if command -v py >/dev/null 2>&1 \
        && [[ "$(python_version py "-${REQUIRED_PYTHON}")" == "${REQUIRED_PYTHON}" ]]; then
        echo "py -${REQUIRED_PYTHON}"
    fi
}

file_hash() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

prepare_venv() {
    local python_bin current_version base_python expected_hash
    python_bin="$(venv_python)"

    if [[ -n "${python_bin}" ]]; then
        current_version="$(python_version "${python_bin}")"
        if [[ "${current_version}" != "${REQUIRED_PYTHON}" ]]; then
            echo "[준비] .venv가 Python ${current_version:-알 수 없음}(으)로 만들어져 ${REQUIRED_PYTHON}(으)로 다시 만듭니다."
            rm -rf "${VENV_DIR}"
            python_bin=""
        fi
    fi

    if [[ -z "${python_bin}" ]]; then
        base_python="$(find_base_python)"
        if [[ -z "${base_python}" ]]; then
            echo "Python ${REQUIRED_PYTHON}을(를) 찾지 못했습니다. 설치한 뒤 다시 실행하세요." >&2
            echo "  macOS: brew install python@${REQUIRED_PYTHON}" >&2
            echo "  Windows: https://www.python.org/downloads/ 에서 ${REQUIRED_PYTHON} 설치" >&2
            exit 1
        fi
        echo "[준비] ${base_python}(으)로 .venv를 만듭니다."
        # "py -3.11"처럼 인자가 붙은 명령도 실행하도록 따옴표 없이 펼친다.
        ${base_python} -m venv "${VENV_DIR}"
        python_bin="$(venv_python)"
    fi

    expected_hash="$(file_hash pyproject.toml)"
    if [[ ! -f "${DEPS_STAMP}" || "$(cat "${DEPS_STAMP}")" != "${expected_hash}" ]] \
        || ! "${python_bin}" -c 'import pytest, ruff' >/dev/null 2>&1; then
        echo "[준비] 개발 의존성을 설치합니다. (pip install -e '.[dev]')"
        if ! "${python_bin}" -m pip install --quiet -e '.[dev]'; then
            echo "의존성 설치에 실패했습니다. 네트워크 연결과 위 pip 오류를 확인하세요." >&2
            exit 1
        fi
        echo "${expected_hash}" > "${DEPS_STAMP}"
    fi

    PYTHON="${python_bin}"
}

if [[ -z "${PYTHON:-}" ]]; then
    prepare_venv
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
