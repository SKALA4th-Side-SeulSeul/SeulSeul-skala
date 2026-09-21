#!/usr/bin/env bash
# 운영 공지 등록·수정·삭제 대화형 도우미. Slack 봇 프로세스는 시작하지 않는다.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    echo '사용법: ./notice.sh [--limit 1~100] [--workspace-id ID]'
    echo '등록·수정·삭제 중 하나를 선택하고 입력값을 확인한 뒤 저장합니다.'
    echo '운영 Compose·Rootless Docker 전용. 기존 manual CLI와 notice_edit.sh도 유지합니다.'
    exit 0
fi

require_operations
# 입력은 컨테이너 stdin으로 전달하고 TTY 자동 할당만 끈다. 셸이 입력값을 해석하지 않는다.
compose run --rm --no-deps -T bot python -m seulseul.notices.manual_console "$@"
