#!/usr/bin/env bash
# 운영자 관리 진입점. 터미널에서 인자 없이 실행하면 전체 화면 관리 콘솔(scripts/console.sh)을,
# 파이프·작은 창·--simple이면 같은 메뉴를 줄 단위로 보여 준다.
# notice·retry·announce·backup 하위 명령은 scripts/의 운영 스크립트에 인자를 그대로 넘긴다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"
source "$ops_root/scripts/menu.sh"

usage() {
    ui_banner '슬슬 관리자' '사용법'
    ui_section '처음이라면 이것만 기억하세요'
    ui_usage_row './admin.sh' '관리 콘솔: 상태·차트를 보며 메뉴를 골라 실행'
    ui_usage_row './admin.sh --simple' '같은 메뉴를 줄 단위로 (작은 창·느린 접속용)'
    ui_section '바로 실행하는 명령 (메뉴와 같은 기능)'
    ui_usage_row './admin.sh notice' '공지 직접 등록·수정·삭제'
    ui_usage_row './admin.sh retry' 'AI 분석에 실패한 공지 목록'
    ui_usage_row './admin.sh retry N' 'N번 공지를 AI로 다시 분석 (N은 대시보드 번호)'
    ui_usage_row './admin.sh retry pending' '반영되지 않은 Slack 원본 보기'
    ui_usage_row './admin.sh announce' '가입 학생 전체에게 안내 DM 보내기'
    ui_usage_row './admin.sh backup' '지금 운영 DB 백업'
    ui_usage_row './admin.sh backup status' '자동 백업 예약과 최근 백업 확인'
    ui_usage_row './admin.sh backup schedule' '매일 03:00 자동 백업 켜기'
    ui_usage_row './admin.sh backup unschedule' '자동 백업 끄기'
    ui_section '다른 운영 명령'
    ui_usage_row './view.sh dashboard' '공지 처리 현황 (조회만)'
    ui_usage_row './view.sh logs bot --follow' '봇 로그 실시간 보기'
    ui_usage_row './run.sh  /  ./stop.sh' '서비스 시작·재배포 / 중지'
    ui_footer '명령 뒤에 --help를 붙이면 자세한 설명이 나옵니다. 예: ./admin.sh backup --help'
}

simple=false
case "${1:-}" in
    "") ;;
    --simple)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        simple=true
        ;;
    --help | -h | help)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        usage
        exit 0
        ;;
    notice | retry | announce | backup)
        tool="$1"
        shift
        exec bash "$ops_root/scripts/$tool.sh" "$@"
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

# 전체 화면은 실제 터미널이고 80×24 이상일 때만 연다.
console_fits() {
    local size
    [[ -t 0 && -t 1 && "${TERM:-dumb}" != dumb ]] || return 1
    size="$(stty size 2>/dev/null || true)"
    [[ "$size" =~ ^([0-9]+)\ ([0-9]+)$ ]] || return 1
    ((BASH_REMATCH[1] >= 24 && BASH_REMATCH[2] >= 80))
}
if [[ "$simple" == false ]] && console_fits; then
    exec bash "$ops_root/scripts/console.sh"
fi

require_operations

show_home() {
    ui_clear
    ui_banner '슬슬 관리자' "$(ui_now) 기준"
    ui_section '서비스'
    show_services
    echo
    if ! compose run --rm --no-deps -T bot python -m seulseul.notices.dashboard --panel | ui_paint; then
        ui_fail '공지 정보를 조회하지 못했습니다. 위 서비스 상태와 DB를 확인하세요.'
    fi
}

show_menu() {
    local i group=''
    ui_section '무엇을 할까요? 번호를 입력하세요'
    for i in "${!menu_keys[@]}"; do
        if [[ "${menu_groups[i]}" != "$group" ]]; then
            group="${menu_groups[i]}"
            printf '\n  %s%s%s\n' "$ui_dim" "$group" "$ui_reset"
        fi
        ui_menu_item "${menu_keys[i]}" "${menu_labels[i]}"
    done
    echo
    printf '    %sh%s 메뉴 설명   %sr%s 새로고침   %sq%s 종료\n\n' \
        "$ui_bold" "$ui_reset" "$ui_bold" "$ui_reset" "$ui_bold" "$ui_reset"
}

show_menu_help() {
    local i
    ui_banner '메뉴 설명'
    for i in "${!menu_keys[@]}"; do
        printf '\n  %s%s  %s%s\n' "$ui_bold" "${menu_keys[i]}" "${menu_labels[i]}" "$ui_reset"
        printf '     %s\n' "${menu_descriptions[i]}"
    done
}

while true; do
    show_home
    show_menu
    menu_read choice '선택: ' || exit 0
    echo
    case "$choice" in
        [0-9]) menu_run "$choice" ;;
        h | H)
            show_menu_help
            menu_pause
            ;;
        r | R | "") ;;
        q | Q)
            ui_ok '관리자 메뉴를 종료합니다.'
            exit 0
            ;;
        *)
            ui_warn '메뉴에 있는 번호(0~9)나 h·r·q 중에서 입력하세요.'
            menu_pause
            ;;
    esac
done
