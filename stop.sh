#!/usr/bin/env bash
# 운영 서비스 중지. 컨테이너와 DB 볼륨은 삭제하지 않는다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    ui_banner '슬슬 서비스 중지' '사용법'
    ui_section '이렇게 실행하세요'
    ui_usage_row './stop.sh' '전체 중지: 봇·OAuth·ngrok → DB 순서'
    ui_usage_row './stop.sh bot' '봇만 중지 (DB 유지)'
    ui_usage_row './stop.sh oauth' 'OAuth 서버와 ngrok만 중지'
    ui_usage_row './stop.sh ngrok' 'ngrok만 중지'
    ui_usage_row './stop.sh postgres' 'DB 중지 (DB를 쓰는 서비스도 먼저 중지)'
    ui_section '안심하세요'
    ui_info '어떤 경우에도 컨테이너와 DB 데이터(볼륨)는 삭제하지 않습니다.'
    ui_footer '다시 시작: ./run.sh' '상태 확인: ./view.sh'
}

[[ $# -le 1 ]] || { usage >&2; exit 2; }
operation="${1:-all}"
case "$operation" in
    --help | -h)
        usage
        exit 0
        ;;
    all) title='전체 (봇·OAuth·ngrok → DB)' ;;
    bot) title='봇만' ;;
    oauth) title='OAuth 서버·ngrok' ;;
    ngrok) title='ngrok만' ;;
    postgres) title='DB와 DB를 쓰는 서비스' ;;
    *)
        usage >&2
        exit 2
        ;;
esac
require_operations
ui_banner '슬슬 서비스 중지' "$title"
case "$operation" in
    oauth)
        ui_step 1 1 'OAuth 서버·ngrok 중지'
        compose stop ngrok oauth
        ;;
    bot)
        ui_step 1 1 '봇 중지'
        compose stop bot
        ;;
    ngrok)
        ui_step 1 1 'ngrok 중지'
        compose stop ngrok
        ;;
    postgres)
        ui_step 1 1 '봇·OAuth·ngrok·DB 중지'
        compose stop bot oauth ngrok postgres
        ;;
    all)
        ui_step 1 2 '봇·OAuth·ngrok 중지'
        compose stop bot oauth ngrok
        ui_step 2 2 'DB 중지'
        compose stop postgres
        ;;
esac
echo
ui_ok '중지했습니다. DB 데이터와 볼륨은 그대로 보존했습니다.'
ui_section '서비스 상태'
show_services
ui_footer '다시 시작: ./run.sh'
