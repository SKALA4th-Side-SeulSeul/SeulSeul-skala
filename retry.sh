#!/usr/bin/env bash
# 운영자가 실패 공지를 조회·수동 재처리하는 진입점.
# 실행 중인 봇을 두 번째로 시작하지 않고 일회성 컨테이너에서 CLI를 실행한다.

set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    cat <<'USAGE'
사용법:
  ./retry.sh list [--limit N] [--workspace-id ID]
  ./retry.sh pending [--limit N] [--workspace-id ID]
  ./retry.sh retry --workspace-id ID --url URL [--channel-id ID] [--message-ts TS]

설명:
  list     AI 분석에 실패한 공지와 자동 재처리 예약을 조회합니다. (기본 동작)
  pending  아직 적용되지 않은 Slack 원본 식별자를 조회합니다.
  retry    선택한 실패 공지 하나를 즉시 다시 분석합니다.

URL은 Slack 메시지 링크가 아니라 공지에 포함된 제출 링크(forms/docs)입니다.
USAGE
}

action="${1:-list}"
if [[ $# -gt 0 ]]; then
    shift
fi

case "${action}" in
    --help|-h)
        usage
        exit 0
        ;;
    list|pending|retry)
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

require_operations
compose run --rm --no-deps -T bot \
    python -m seulseul.notices.retry "${action}" "$@"
