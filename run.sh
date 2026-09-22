#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    echo '사용법: ./run.sh [all|postgres|migrate|setup|--help]'
    echo '  all(기본): 이미지 빌드 → 봇·OAuth·ngrok 중지 → DB 준비 → 마이그레이션 → 재생성'
    echo '  postgres: DB만 준비 (봇은 새로 실행하지 않음)'
    echo '  migrate: 이미지 빌드 → 봇·OAuth·ngrok 중지 → DB 준비 → 마이그레이션 (서비스 중지 유지)'
    echo '  setup: 없는 경우에만 운영 .env 템플릿 생성. 이후 직접 편집 필요'
}
[[ $# -le 1 ]] || { usage; exit 2; }
operation="${1:-all}"
case "$operation" in
    --help|-h) usage; exit 0 ;;
    setup)
        if [[ -e .env || -L .env ]]; then
            echo '기존 .env는 변경하지 않았습니다. nano .env로 편집하세요.'
        else
            # noclobber로 동시 실행 시에도 기존 설정을 덮어쓰지 않는다.
            (umask 077; set -C; cat .env.example > .env)
            echo '.env 템플릿 생성 완료. nano .env로 운영 값을 입력하세요.'
        fi
        echo 'APP_ENV=production, AI_PROVIDER=nvidia, DB 호스트=postgres:5432'
        echo 'AI_TIMEOUT_SECONDS=45, NVIDIA_API_KEY에는 발급한 키를 입력하세요.'
        echo '외부 설치에는 Slack OAuth용 환경변수 4개와 NGROK_AUTHTOKEN, NGROK_DOMAIN이 필요합니다.'
        exit 0 ;;
    all|postgres|migrate) ;;
    *) usage; exit 2 ;;
esac
require_operations
if [[ "$operation" != postgres ]]; then
    echo '동일 Slack 앱의 로컬 봇을 종료했는지 확인하세요. 기존 운영 DB는 먼저 백업하세요.'
    compose build bot migrate oauth
    compose stop bot oauth ngrok
fi
compose up -d --wait --wait-timeout 180 postgres
if [[ "$operation" != postgres ]]; then
    # 실패하면 set -e로 중단. 구버전 봇을 자동으로 다시 켜지 않는다.
    compose run --rm migrate
    if [[ "$operation" == all ]]; then
        compose up -d --no-deps --force-recreate --scale bot=1 bot oauth ngrok
        echo '봇·OAuth·ngrok 시작 요청 완료. ./view.sh logs로 연결·오류를 확인하세요.'
    else
        echo '마이그레이션 완료. 봇·OAuth는 중지 상태입니다. 시작하려면 ./run.sh를 실행하세요.'
    fi
fi
compose ps -a
