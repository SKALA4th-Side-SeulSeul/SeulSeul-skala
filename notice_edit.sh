#!/usr/bin/env bash
# 운영 DB의 공지를 대화형으로 수정한다. Slack 봇을 추가로 시작하지 않는다.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    echo '사용법: ./notice_edit.sh [--limit 1~100] [--workspace-id ID]'
    echo '최근 공지를 번호로 선택하고 바꿀 값만 입력합니다. Enter는 유지, q는 취소입니다.'
    echo '저장 전 변경 내용을 확인하며 원문·링크·완료 기록은 보존합니다.'
    echo '운영 Compose·Rootless Docker 전용. 기존 add/edit/delete 명령도 계속 사용할 수 있습니다.'
    exit 0
fi

require_operations
# stdin은 입력 도우미에 연결하고 TTY 자동 할당만 끈다. 입력값은 셸로 해석하지 않는다.
compose run --rm --no-deps -T bot python -m seulseul.notices.manual_edit "$@"
