#!/usr/bin/env bash
# 운영 서비스 시작·재배포. 실패하면 구버전 봇을 자동으로 다시 켜지 않는다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    ui_banner '슬슬 서비스 시작' '사용법'
    ui_section '이렇게 실행하세요'
    ui_usage_row './run.sh' '전체 재배포: 빌드 → 봇 중지 → DB 준비 → 마이그레이션 → 재시작'
    ui_usage_row './run.sh postgres' 'DB만 준비합니다. 봇은 새로 실행하지 않습니다.'
    ui_usage_row './run.sh migrate' '빌드·마이그레이션까지만. 봇은 중지 상태로 둡니다.'
    ui_usage_row './run.sh setup' '처음 한 번: 운영 .env 템플릿 생성 (기존 파일 보존)'
    ui_section '함께 쓰는 명령'
    ui_usage_row './stop.sh' '서비스 중지 (데이터 보존)'
    ui_usage_row './view.sh' '서비스 상태·대시보드·로그'
    ui_usage_row './admin.sh' '공지·재처리·안내 DM·백업 관리 메뉴'
}

[[ $# -le 1 ]] || { usage >&2; exit 2; }
operation="${1:-all}"
case "$operation" in
    --help | -h)
        usage
        exit 0
        ;;
    setup)
        ui_banner '슬슬 운영 설정 준비' '.env 템플릿'
        echo
        if [[ -e .env || -L .env ]]; then
            ui_ok '기존 .env가 있어 변경하지 않았습니다.'
        else
            # noclobber로 동시 실행 시에도 기존 설정을 덮어쓰지 않는다.
            (umask 077; set -C; cat .env.example > .env)
            ui_ok '.env 템플릿을 만들었습니다 (권한 600).'
        fi
        ui_section '입력할 값'
        ui_info 'APP_ENV=production, AI_PROVIDER=nvidia, DB 호스트=postgres:5432'
        ui_info 'AI_TIMEOUT_SECONDS=45, NVIDIA_API_KEY에는 발급한 키'
        ui_info '외부 설치에는 Slack OAuth 환경변수 4개와 NGROK_AUTHTOKEN, NGROK_DOMAIN'
        ui_footer '편집: nano .env' '설정 검증: ./view.sh config' '시작: ./run.sh'
        exit 0
        ;;
    all | postgres | migrate) ;;
    *)
        usage >&2
        exit 2
        ;;
esac

case "$operation" in
    all) title='전체 재배포' total=5 ;;
    migrate) title='마이그레이션만 (봇 중지 유지)' total=4 ;;
    postgres) title='DB만 준비' total=1 ;;
esac
step=0
step_name=''
next_step() {
    step=$((step + 1))
    step_name="$1"
    ui_step "$step" "$total" "$1"
}
# 어느 단계에서 멈췄는지 알려 준다. 봇은 자동으로 다시 켜지 않는다.
report_failure() {
    local status=$?
    if [[ $status -ne 0 && -n "$step_name" ]]; then
        echo >&2
        ui_fail "[$step/$total] ${step_name} 단계에서 실패했습니다. 봇을 자동으로 다시 켜지 않았습니다."
        ui_hint '원인 확인: 위 오류 메시지 또는 ./view.sh logs all' >&2
        ui_hint '원인을 고친 뒤 같은 명령을 다시 실행하세요.' >&2
    fi
}
trap report_failure EXIT

require_operations
ui_banner '슬슬 서비스 시작' "$title"
if [[ "$operation" != postgres ]]; then
    echo
    ui_warn '같은 Slack 앱의 로컬 봇이 켜져 있다면 먼저 종료하세요.'
    ui_warn '운영 데이터가 있다면 먼저 백업하세요: ./admin.sh backup'
    next_step '이미지 빌드 (bot·migrate·oauth)'
    compose build bot migrate oauth
    next_step '기존 봇·OAuth·ngrok 중지'
    compose stop bot oauth ngrok
fi
next_step 'DB 준비 (정상 상태까지 최대 180초 대기)'
compose up -d --wait --wait-timeout 180 postgres
if [[ "$operation" != postgres ]]; then
    next_step 'DB 마이그레이션'
    compose run --rm migrate
    if [[ "$operation" == all ]]; then
        next_step '봇·OAuth·ngrok 시작'
        compose up -d --no-deps --force-recreate --scale bot=1 bot oauth ngrok
    fi
fi
step_name=''

echo
case "$operation" in
    all) ui_ok '봇·OAuth·ngrok 시작을 요청했습니다.' ;;
    migrate) ui_ok '마이그레이션을 마쳤습니다. 봇·OAuth는 중지 상태입니다.' ;;
    postgres) ui_ok 'DB가 준비되었습니다.' ;;
esac
ui_section '서비스 상태'
show_services
case "$operation" in
    all) ui_footer '연결·오류 확인: ./view.sh logs bot --follow' '공지 처리 현황: ./view.sh dashboard' ;;
    migrate) ui_footer '봇까지 시작: ./run.sh' ;;
    postgres) ui_footer '전체 시작: ./run.sh' ;;
esac
