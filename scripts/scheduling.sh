#!/usr/bin/env bash
# 예약 관리 전용 공통 코드. 실제 백업은 backup.sh에만 둔다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/operations.sh"
schedule_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
schedule_marker='# Managed by SeulSeul backup scheduling scripts'

schedule_arguments() {
    if [[ $# -eq 0 ]]; then return; fi
    echo '사용법: ./backup_run.sh (매일 한국 시간 03시 자동 백업 등록)'
    echo '        ./backup_stop.sh (예약 해제, 실행 중인 백업은 유지)'
    if [[ $# -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then exit 0; fi
    exit 2
}

require_scheduler() {
    command -v systemctl >/dev/null || { echo 'Ubuntu systemd 사용자 세션이 필요합니다.' >&2; exit 1; }
    systemctl --user show-environment >/dev/null
}

check_schedule_files() {
    local unit path first_line
    for unit in service timer; do
        path="$schedule_dir/seulseul-db-backup.$unit"
        if [[ -L "$path" || ( -e "$path" && ! -f "$path" ) ]]; then
            echo "안전하지 않은 예약 파일: $path" >&2
            exit 1
        fi
        if [[ -f "$path" ]]; then
            first_line=''
            IFS= read -r first_line < "$path" || true
            if [[ "$first_line" != "$schedule_marker" ]]; then
                echo "직접 작성된 예약 파일은 덮어쓰거나 중지하지 않습니다: $path" >&2
                exit 1
            fi
        fi
    done
}
