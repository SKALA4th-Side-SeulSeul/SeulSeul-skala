#!/usr/bin/env bash
# 실패 공지 확인·AI 재분석. ./admin.sh retry 로 실행한다.
# 실행 중인 봇을 두 번째로 시작하지 않고 일회성 컨테이너에서 CLI를 실행한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"

# 대시보드(100건 조회)와 같은 번호를 쓰도록 기본 조회 한도를 맞춘다.
# 사용자가 --limit을 다시 주면 argparse 규칙상 뒤의 값이 우선한다.
default_limit=(--limit 100)

usage() {
    ui_banner '슬슬 실패 공지 재처리' '사용법'
    ui_section '자주 쓰는 명령'
    ui_usage_row './admin.sh retry' 'AI 분석에 실패한 공지 목록 (번호 확인)'
    ui_usage_row './admin.sh retry 3' '3번 공지를 AI로 다시 분석'
    ui_usage_row './admin.sh retry pending' '아직 반영되지 않은 Slack 원본 보기'
    ui_section '고급: 번호 대신 식별자로 지정'
    ui_usage_row './admin.sh retry retry --workspace-id ID --url URL' ''
    ui_info '--url은 Slack 메시지 링크가 아니라 공지 안의 제출 링크(forms/docs)입니다.'
    ui_info '--channel-id ID --message-ts TS를 함께 주면 원본을 정확히 고릅니다.'
    ui_section '알아두세요'
    ui_info '번호는 ./view.sh dashboard 의 처리가 필요한 공지 번호와 같습니다.'
    ui_info '다시 분석은 AI를 호출합니다. 제목·마감일을 직접 고치려면 ./admin.sh notice'
}

action="${1:-list}"
if [[ $# -gt 0 ]]; then
    shift
fi
case "$action" in
    --help | -h)
        usage
        exit 0
        ;;
    list | pending)
        cli_args=("$action" "${default_limit[@]}" "$@")
        ;;
    retry)
        cli_args=(retry "${default_limit[@]}" "$@")
        ;;
    *)
        if [[ ! "$action" =~ ^[1-9][0-9]*$ ]]; then
            usage >&2
            exit 2
        fi
        cli_args=(retry --index "$action" "${default_limit[@]}" "$@")
        ;;
esac

require_operations
if [[ "${cli_args[0]}" == retry ]]; then
    ui_info 'AI로 다시 분석하는 중입니다. 응답을 기다리느라 1~2분 걸릴 수 있습니다.'
fi
compose run --rm --no-deps -T bot python -m seulseul.notices.retry "${cli_args[@]}" | ui_paint
