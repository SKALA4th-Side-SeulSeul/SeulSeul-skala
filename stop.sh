#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    echo '사용법: ./stop.sh [all|bot|oauth|ngrok|postgres|--help]'
    echo '  all(기본): 봇·OAuth·ngrok 서버 → DB 순서로 중지. 컨테이너·DB 볼륨은 삭제하지 않음'
    echo '  bot: 봇만 중지 / oauth: OAuth·ngrok 중지 / ngrok: ngrok만 중지 / postgres: DB 의존 서비스도 먼저 중지'
}
[[ $# -le 1 ]] || { usage; exit 2; }
operation="${1:-all}"
case "$operation" in
    --help|-h) usage; exit 0 ;;
    all|bot|oauth|ngrok|postgres) ;;
    *) usage; exit 2 ;;
esac
require_operations
if [[ "$operation" == oauth ]]; then
    compose stop ngrok oauth
elif [[ "$operation" == bot ]]; then
    compose stop bot
elif [[ "$operation" == ngrok ]]; then
    compose stop ngrok
elif [[ "$operation" == postgres ]]; then
    compose stop bot oauth ngrok postgres
else
    compose stop bot oauth ngrok
    compose stop postgres
fi
echo '중지 완료. DB 데이터와 볼륨은 보존했습니다.'
compose ps -a
