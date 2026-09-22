#!/usr/bin/env bash
# 운영자가 대시보드에서 바로 조치하는 대화형 진입점.
# 모든 실제 작업은 기존 운영 스크립트에 위임한다.
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/operations.sh"

usage() {
    cat <<'USAGE'
사용법: ./admin.sh

메뉴:
  1  실패 공지 번호를 선택해 AI 재처리
  2  공지 수동 등록·수정·삭제
  3  미적용 Slack 원본 조회
  4  봇 상세 로그 실시간 보기
  r  대시보드 새로고침
  q  종료

직접 실행:
  ./view.sh dashboard
  ./view.sh logs bot --follow
  ./retry.sh retry --index N --limit 100
  ./notice.sh
USAGE
}

case "${1:-}" in
    --help|-h)
        [[ $# -eq 1 ]] || { usage >&2; exit 2; }
        usage
        exit 0
        ;;
    "")
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

require_operations

pause_menu() {
    local ignored
    IFS= read -r -p 'Enter를 누르면 메뉴로 돌아갑니다: ' ignored || true
}

read_value() {
    local variable="$1"
    local prompt="$2"
    local value
    if ! IFS= read -r -p "$prompt" value; then
        echo
        exit 0
    fi
    printf -v "$variable" '%s' "$value"
}

show_dashboard() {
    if [[ -t 1 ]]; then
        printf '\033[2J\033[H'
    fi
    if ! bash ./view.sh dashboard; then
        echo '대시보드를 조회하지 못했습니다. DB와 운영 설정을 확인하세요.' >&2
        pause_menu
    fi
}

while true; do
    show_dashboard
    cat <<'MENU'

[관리 메뉴]
  1) 실패 공지 AI 재처리
  2) 공지 수동 등록·수정·삭제
  3) 미적용 원본 조회
  4) 봇 상세 로그 실시간 보기
  r) 대시보드 새로고침
  q) 종료
MENU
    read_value choice '선택: '
    case "$choice" in
        1)
            read_value index '재처리할 번호: '
            if [[ ! "$index" =~ ^[1-9][0-9]*$ ]]; then
                echo '번호는 1 이상의 숫자로 입력하세요.'
                pause_menu
                continue
            fi
            read_value confirm "번호 ${index} 공지를 AI 재처리할까요? [y/N]: "
            if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
                echo '재처리를 취소했습니다.'
                continue
            fi
            if ! bash ./retry.sh retry --index "$index" --limit 100; then
                :
            fi
            pause_menu
            ;;
        2)
            if ! bash ./notice.sh --limit 100; then
                :
            fi
            ;;
        3)
            if ! bash ./retry.sh pending --limit 100; then
                :
            fi
            pause_menu
            ;;
        4)
            if ! bash ./view.sh logs bot --follow; then
                :
            fi
            ;;
        r|R)
            ;;
        q|Q)
            echo '관리자 메뉴를 종료합니다.'
            exit 0
            ;;
        *)
            echo '1~4, r, q 중에서 선택하세요.'
            pause_menu
            ;;
    esac
done
