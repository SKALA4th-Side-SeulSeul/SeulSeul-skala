#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    echo '사용법: ./view.sh [status|logs [bot|postgres|all]|db|schema|config|--help]'
    echo '  status(기본): 컨테이너 상태 / logs: 최근 100줄 및 실시간 로그'
    echo '  db: 읽기 전용 psql 접속 (종료: \q) / schema: 테이블 목록'
    echo '  config: 값 출력 없이 Compose 설정 검증'
    echo '  로그 공유 전 토큰·개인정보를 가리세요. Ctrl+C는 로그 보기만 종료합니다.'
}
[[ $# -le 2 ]] || { usage; exit 2; }
operation="${1:-status}"
case "$operation" in
    --help|-h) usage; exit 0 ;;
    status|db|schema|config)
        [[ $# -le 1 ]] || { usage; exit 2; } ;;
    logs)
        case "${2:-bot}" in bot|postgres|all) ;; *) usage; exit 2 ;; esac ;;
    *) usage; exit 2 ;;
esac
require_operations
case "$operation" in
    status) compose ps -a ;;
    logs)
        if [[ "${2:-bot}" == all ]]; then
            compose logs --tail 100 -f bot postgres
        else
            compose logs --tail 100 -f "${2:-bot}"
        fi ;;
    db)
        compose exec -e PGOPTIONS='-c default_transaction_read_only=on' postgres \
            sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB"' ;;
    schema)
        compose exec -T -e PGOPTIONS='-c default_transaction_read_only=on' postgres \
            sh -c 'exec psql -X -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"' ;;
    config) echo 'Compose 설정 검증 통과 (DB 연결·AI 키 유효성 검증은 아님).' ;;
esac
