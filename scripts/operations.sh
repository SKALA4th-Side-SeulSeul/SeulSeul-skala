#!/usr/bin/env bash
# 운영 Compose 공통 경계. .env를 shell source하거나 출력하지 않는다.
set -euo pipefail

ops_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ops_root"

compose() {
    docker compose --project-directory "$ops_root" --env-file "$ops_root/.env" \
        -f "$ops_root/compose.prod.yaml" "$@"
}

require_operations() {
    if [[ ! -f .env ]]; then
        echo '운영 .env가 없습니다. ./run.sh setup 후 값을 설정하세요.' >&2
        exit 1
    fi
    command -v docker >/dev/null || { echo 'Docker가 필요합니다.' >&2; exit 1; }
    docker compose version >/dev/null
    local security
    security="$(docker info --format '{{json .SecurityOptions}}')"
    if [[ "$security" != *'name=rootless'* ]]; then
        echo '운영 정책상 Rootless Docker가 필요합니다. seulseul 계정으로 직접 로그인하세요.' >&2
        exit 1
    fi
    compose config --quiet
}
