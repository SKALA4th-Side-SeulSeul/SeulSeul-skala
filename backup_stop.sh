#!/usr/bin/env bash
# 예약만 해제한다. 이미 실행 중인 백업·기존 파일·DB는 유지한다.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/scheduling.sh"
schedule_arguments "$@"
require_scheduler
check_schedule_files
if [[ ! -f "$schedule_dir/seulseul-db-backup.timer" ]]; then
    echo '등록된 자동 백업이 없습니다.'
    exit 0
fi
systemctl --user disable --now seulseul-db-backup.timer
echo '자동 백업 예약을 해제했습니다. 실행 중인 백업과 기존 백업 파일·DB는 유지됩니다.'
