#!/usr/bin/env bash
# 현재 가입 학생에게 운영 안내 DM을 수동 발송한다. ./admin.sh announce 로 실행한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"

usage() {
    ui_banner '슬슬 안내 DM 발송' '가입 학생 전체'
    ui_section '이렇게 실행하세요'
    ui_usage_row './admin.sh announce' '안내문 입력 → 미리보기·수신 인원 확인 → 발송'
    ui_section '진행 순서'
    ui_info '안내문을 입력합니다. 줄바꿈은 Enter, 입력을 마치려면 빈 줄에서 Ctrl+D'
    ui_info '미리보기와 수신 인원을 확인하고 y를 입력해야 발송합니다.'
    ui_info '취소는 Ctrl+C. 운영 DB가 실행 중이고 bot 이미지가 빌드되어 있어야 합니다.'
    ui_section '주의'
    ui_warn '다시 실행하면 이미 받은 학생에게도 또 발송됩니다. 결과를 확인하고 실행하세요.'
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    usage
    exit 0
fi
require_operations
ui_banner '슬슬 안내 DM 발송' '가입 학생 전체'
ui_warn '다시 실행하면 이미 받은 학생에게도 또 발송됩니다.'
ui_info '입력을 마치려면 빈 줄에서 Ctrl+D · 취소는 Ctrl+C'
echo
# Ctrl+D 이후에도 확인 입력을 받을 수 있도록 파이프가 아닌 터미널을 연결한다.
compose run --rm --no-deps --interactive --tty bot python -m seulseul.users.announce "$@"
