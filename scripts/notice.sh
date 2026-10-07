#!/usr/bin/env bash
# 운영 공지 등록·수정·삭제 대화형 도우미. ./admin.sh notice 로 실행한다. Slack 봇 프로세스는 시작하지 않는다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"

usage() {
    ui_banner '슬슬 공지 관리' '등록·수정·삭제'
    ui_section '이렇게 실행하세요'
    ui_usage_row './admin.sh notice' '최근 공지 20건에서 등록·수정·삭제 선택'
    ui_usage_row './admin.sh notice --limit 100' '최근 100건까지 보기 (1~100)'
    ui_usage_row './admin.sh notice --workspace-id T...' '특정 워크스페이스만 보기'
    ui_section '진행 순서'
    ui_info '1 등록 · 2 수정 · 3 삭제 중 하나를 고릅니다.'
    ui_info '수정은 공지 번호를 고르고 바꿀 값만 입력합니다. Enter는 기존 값 유지입니다.'
    ui_info '마지막 확인에서 y를 입력해야 저장합니다. 학생별 완료 기록은 보존됩니다.'
    ui_section '봇이 없는 채널의 공지'
    ui_info '원문 채널이 설정에 없으면 학생 배정 기준 채널을 번호로 고릅니다.'
    ui_info '고른 채널의 대상(광주 전체·N반)에게 보이고, 원문 링크는 실제 메시지 그대로 저장합니다.'
    ui_info '워크스페이스는 봇 토큰 기준으로 자동 확인하며 실패할 때만 묻습니다.'
    ui_footer '처리 결과·실패 원인 기록: ./view.sh logs manual' \
        '원문 파일을 직접 넘기는 기존 manual CLI도 유지합니다 (README 참고).'
}

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    usage
    exit 0
fi

require_operations
prepare_manual_log
ui_banner '슬슬 공지 관리' '등록·수정·삭제'
ui_info '언제든 q 또는 Ctrl+C로 저장하지 않고 취소할 수 있습니다.'
ui_info '봇이 없는 채널의 원문도 등록할 수 있습니다. 워크스페이스는 자동으로 확인합니다.'
ui_info '처리 결과·실패 원인은 기록으로 남습니다: ./view.sh logs manual'
echo
# 입력은 컨테이너 stdin으로 전달하고 TTY 자동 할당만 끈다. 셸이 입력값을 해석하지 않는다.
# 화면 출력(fd 1)은 그대로 터미널로 보내고, 로그·오류(fd 2)만 기록 파일에도 덧붙인다.
# pipefail이라 콘솔의 종료 코드가 그대로 전달된다.
{
    compose run --rm --no-deps -T bot python -m seulseul.notices.manual_console "$@" \
        2>&1 1>&3 3>&- | tee -a "$manual_log_file" 1>&2 3>&-
} 3>&1
